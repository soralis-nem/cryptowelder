from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import new
from threading import Thread, Lock
from time import sleep
from urllib.parse import urlencode

from cryptowelder.context import CryptowelderContext, Ticker, Balance, AccountType, UnitType, Transaction, \
    TransactionType


class BitmexWelder:
    _ID = 'bitmex'
    _ZERO = Decimal('0')
    _SATOSHI = Decimal('0.00000001')

    def __init__(self, context):
        self.__context = context
        self.__logger = context.get_logger(self)
        self.__endpoint = self.__context.get_property(self._ID, 'endpoint', 'https://www.bitmex.com')
        self.__thread = Thread(daemon=False, target=self._loop)
        self.__lock = Lock()
        self.__code_cache = {}

    def run(self):

        self.__thread.start()

    def _join(self):

        self.__thread.join()

    def _loop(self, *, default_interval=15):

        self.__logger.info('Processing : %s', self.__endpoint)

        while not self.__context.is_closed():

            threads = [
                Thread(target=self._process_ticker),
                Thread(target=self._process_margin),
                Thread(target=self._process_transaction),
            ]

            for t in threads:
                t.start()

            for t in threads:
                t.join()

            sleep(float(self.__context.get_property(self._ID, 'interval', default_interval)))

        self.__logger.info('Terminated.')

    def _process_ticker(self):

        try:

            now = self.__context.get_now()

            response = self.__context.requests_get(self.__endpoint + '/api/v1/instrument/activeAndIndices')

            if response is None:
                return

            values = []

            codes = self.__context.get_property(
                self._ID, 'codes', '.BXBT'
            ).split(',')

            references = self.__context.get_property(
                self._ID, 'references', '.BXBT,.BXBT30M,.ETHXBT30M,.BCHXBT30M'
            ).split(',')

            for instrument in response:

                code = instrument.get('symbol')

                reference = instrument.get('referenceSymbol')

                state = instrument.get('state')

                multiplier = instrument.get('multiplier')

                if (
                        code is None or code not in codes
                ) and (
                        reference is None or reference not in references or state == 'Unlisted'
                ):
                    continue

                # Cache ticker symbols for transaction query.
                if state != 'Unlisted':
                    self.__code_cache[code] = (now, multiplier)

                ticker = Ticker()
                ticker.tk_site = self._ID
                ticker.tk_code = code
                ticker.tk_time = self.__context.parse_iso_timestamp(instrument.get('timestamp'))
                ticker.tk_ask = instrument.get('askPrice')
                ticker.tk_bid = instrument.get('bidPrice')
                ticker.tk_ltp = instrument.get('lastPrice')

                values.append(ticker)

            self.__context.save_tickers(values)

            for v in values:
                self.__logger.debug('Ticker : %s', v)

        except Exception as e:

            self.__logger.warn('Ticker Failure : %s - %s', type(e), e.args)

    def _query_private(self, path):

        apikey = self.__context.get_property(self._ID, 'apikey', None)
        secret = self.__context.get_property(self._ID, 'secret', None)

        if apikey is None or secret is None:
            return None

        with self.__lock:
            timestamp = str(int(self.__context.get_nonce(self._ID).timestamp() * 1000))

            data = 'GET' + path + timestamp

            digest = new(str.encode(secret), str.encode(data), sha256).hexdigest()

            headers = {
                "api-key": apikey,
                "api-nonce": timestamp,
                "api-signature": digest,
                "Accept": "application/json"
            }

            return self.__context.requests_get(self.__endpoint + path, headers=headers)

    def _process_margin(self):

        try:

            now = self.__context.get_now()

            balances = self._query_private('/api/v1/user/margin?currency=all')

            values = []

            for balance in balances if balances is not None else []:

                if 'XBt' != balance.get('currency'):
                    continue

                value = Balance()
                value.bc_site = self._ID
                value.bc_acct = AccountType.MARGIN
                value.bc_unit = UnitType.BTC
                value.bc_time = now
                value.bc_amnt = balance.get('walletBalance') * self._SATOSHI

                values.append(value)

            self.__context.save_balances(values)

            for value in values:
                self.__logger.debug('Margin : %s', value)

        except Exception as e:

            self.__logger.warn('Margin Failure : %s - %s', type(e), e.args)

    def _process_transaction(self):

        cutoff = self.__context.get_now() - timedelta(hours=1)

        threads = []

        for code, details in self.__code_cache.items():

            timestamp = details[0]

            if timestamp < cutoff:
                continue

            multiplier = details[1]

            threads.append(Thread(target=self._fetch_transaction, args=(code, multiplier)))

        for t in threads:
            t.start()

        for t in threads:
            t.join()

    def _fetch_transaction(self, code, multiplier, *, limit=100):

        try:

            parameters = {
                'reverse': True,
                'count': limit,
                'start': 0,
                'symbol': code,
            }

            while True:

                executions = self._query_private('/api/v1/execution/tradeHistory?' + urlencode(parameters))

                values = []

                for execution in executions if executions is not None else []:

                    value = Transaction()
                    value.tx_site = self._ID
                    value.tx_code = execution.get('symbol')
                    value.tx_acct = AccountType.MARGIN
                    value.tx_oid = execution.get('orderID')
                    value.tx_eid = execution.get('execID')
                    value.tx_time = self.__context.parse_iso_timestamp(execution.get('transactTime'))

                    side = execution.get('side', '')
                    comm = Decimal(execution.get('execComm', 0))

                    if side == '':

                        value.tx_type = TransactionType.SWAP

                        if multiplier >= 0:
                            value.tx_inst = self._ZERO
                            value.tx_fund = self._SATOSHI * -comm
                        else:
                            value.tx_inst = self._SATOSHI * -comm
                            value.tx_fund = self._ZERO

                    else:

                        value.tx_type = TransactionType.TRADE

                        sign = +1 if side == 'Buy' else -1 if side == 'Sell' else 0
                        size = Decimal(execution.get('lastQty') * sign)
                        last = Decimal(execution.get('lastPx'))

                        if multiplier >= 0:
                            value.tx_inst = (+size)
                            value.tx_fund = (-size * multiplier * last - comm) * self._SATOSHI
                        else:
                            value.tx_inst = (-size * multiplier / last - comm) * self._SATOSHI
                            value.tx_fund = (-size)

                    values.append(value)

                self.__logger.debug('Transactions - %s : extracted=[%s] offset=[%s]',
                                    code, len(values), parameters.get('start'))

                results = self.__context.save_transactions(values)

                if len(results) <= 0:
                    break

                parameters['start'] = parameters['start'] + len(values)

        except Exception as e:

            self.__logger.warn('Transaction Failure - %s : %s - %s', code, type(e), e.args)


def main():
    context = CryptowelderContext(config='~/.cryptowelder', debug=True)
    context.launch_prometheus()

    target = BitmexWelder(context)
    target.run()


if __name__ == '__main__':
    main()
