from __future__ import division
from __future__ import unicode_literals

import datetime
from decimal import Decimal

import yahoo_finance
from sqlalchemy import Column, VARCHAR, INTEGER, ForeignKey, BIGINT, Index
from sqlalchemy.orm import relation

from ._commodity_helper import quandl_fx
from .._common import CallableList
from .._common import GnucashException, hybrid_property_gncnumeric
from .._declbase import DeclarativeBaseGuid
from ..sa_extra import _DateTime


class GncCommodityError(GnucashException):
    pass


class GncPriceError(GnucashException):
    pass


class Price(DeclarativeBaseGuid):
    """
    A single Price for a commodity.

    Attributes:
        commodity (:class:`Commodity`): commodity to which the Price relates
        currency (:class:`Commodity`): currency in which the Price is expressed
        date (:class:`datetime.datetime`): datetime object representing the time at which the price is relevant
        source (str): source of the price
        type (str): last, ask, bid, unknown, nav
        value (:class:`decimal.Decimal`): the price itself
    """
    __tablename__ = 'prices'

    __table_args__ = {}

    # column definitions
    commodity_guid = Column('commodity_guid', VARCHAR(length=32), ForeignKey('commodities.guid'), nullable=False)
    currency_guid = Column('currency_guid', VARCHAR(length=32), ForeignKey('commodities.guid'), nullable=False)
    date = Column('date', _DateTime, nullable=False)
    source = Column('source', VARCHAR(length=2048))
    type = Column('type', VARCHAR(length=2048))

    _value_num = Column('value_num', BIGINT(), nullable=False)
    _value_denom = Column('value_denom', BIGINT(), nullable=False)
    value = hybrid_property_gncnumeric(_value_num, _value_denom)

    # relation definitions
    commodity = relation('Commodity',
                         back_populates="prices",
                         foreign_keys=[commodity_guid],
                         )
    currency = relation('Commodity',
                        foreign_keys=[currency_guid],
                        )

    def __init__(self,
                 commodity,
                 currency,
                 date,
                 value,
                 type="unknown",
                 source="user:price"):
        self.commodity = commodity
        self.currency = currency
        assert isinstance(date, datetime.datetime)
        self.date = date
        self.value = value
        self.type = type
        self.source = source

    def __unirepr__(self):
        return u"Price<{:%Y-%m-%d} : {} {}/{}>".format(self.date,
                                                       self.value,
                                                       self.currency.mnemonic,
                                                       self.commodity.mnemonic)


class Commodity(DeclarativeBaseGuid):
    """
    A GnuCash Commodity.

    Attributes:
        cusip (str): cusip code
        fraction (int): minimal unit of the commodity (e.g. 100 for 1/100)
        namespace (str): CURRENCY for currencies, otherwise any string to group multiple commodities together
        mnemonic (str): the ISO symbol for a currency or the stock symbol for stocks (used for online quotes)
        quote_flag (int): 1 if piecash/GnuCash quotes will retrieve online quotes for the commodity
        quote_source (str): the quote source for GnuCash (piecash always use yahoo for stock and quandl for currencies
        quote_tz (str): the timezone to assign on the online quotes
        base_currency (:class:`Commodity`): The base_currency for a commodity:

          - if the commodity is a currency, returns the "default currency" of the book (ie the one of the root_account)
          - if the commodity is not a currency, returns the currency encoded in the quoted_currency slot

        accounts (list of :class:`piecash.core.account.Account`): list of accounts which have the commodity as commodity
        transactions (list of :class:`piecash.core.transaction.Transaction`): list of transactions which have the commodity as currency
        prices (iterator of :class:`Price`): iterator on prices related to the commodity (it is a sqlalchemy query underneath)

    """
    __tablename__ = 'commodities'

    __table_args__ = (Index('_unique_cdty',
                            'namespace', 'mnemonic',
                            unique=True,
                            mysql_length={'namespace': 200,
                                          'mnemonic': 10},
                            ),
                      )

    # column definitions
    namespace = Column('namespace', VARCHAR(length=2048), nullable=False)
    mnemonic = Column('mnemonic', VARCHAR(length=2048), nullable=False)
    fullname = Column('fullname', VARCHAR(length=2048))
    cusip = Column('cusip', VARCHAR(length=2048))
    fraction = Column('fraction', INTEGER(), nullable=False)
    quote_flag = Column('quote_flag', INTEGER(), nullable=False)
    quote_source = Column('quote_source', VARCHAR(length=2048))
    quote_tz = Column('quote_tz', VARCHAR(length=2048))

    @property
    def base_currency(self):
        b = self.book
        if b is None:
            raise GnucashException("The commodity should be linked to a session to have a 'base_currency'")

        if self.namespace == "CURRENCY":
            # get the base currency as first commodity in DB
            return b.default_currency
        else:
            # retrieve currency from quoted_currency kvp
            # TODO: recover from the web (as fallback)
            mnemonic = self.get("quoted_currency", None)
            if mnemonic:
                return b.currencies(mnemonic=mnemonic)
            else:
                raise GnucashException("The commodity '{}' has no information about its base currency. "
                                       "Add a kvp item named 'quoted_currency' with the mnemonic of the "
                                       "currency to have proper behavior".format(self.mnemonic))

    # relation definitions
    accounts = relation('Account',
                        back_populates='commodity',
                        cascade='all, delete-orphan',
                        collection_class=CallableList)
    transactions = relation('Transaction',
                            back_populates='currency',
                            cascade='all, delete-orphan',
                            collection_class=CallableList,
                            )
    prices = relation("Price",
                      back_populates='commodity',
                      foreign_keys=[Price.commodity_guid],
                      cascade='all, delete-orphan',
                      lazy="dynamic",
                      )

    def __init__(self,
                 namespace,
                 mnemonic,
                 fullname,
                 fraction=100,
                 cusip="",
                 quote_flag=0,
                 quote_source=None,
                 quote_tz=None,
                 book=None):

        if quote_source is None:
            quote_source = "currency" if namespace == "CURRENCY" else "yahoo"

        if book is not None:
            book.session.add(self)

        self.namespace = namespace
        self.mnemonic = mnemonic
        self.fullname = fullname
        self.fraction = fraction
        self.cusip = cusip
        self.quote_flag = quote_flag
        self.quote_source = quote_source
        self.quote_tz = quote_tz

    def __unirepr__(self):
        return u"Commodity<{}:{}>".format(self.namespace, self.mnemonic)

    def update_prices(self, start_date=None):
        """
        Retrieve online prices for the commodity:

        - for currencies, it will get from quandl the exchange rates between the currency and its base_currency
        - for stocks, it will get from yahoo the daily closing prices expressed in its base_currency

        Args:
            start_date (:class:`datetime.date`): prices will be updated as of the start_date. If None, start_date is today
            - 7 days.

        .. note:: if prices are already available in the GnuCash file, the function will only retrieve prices as of the
           max(start_date, last quoted price date)

        .. todo:: add some frequency to retrieve prices only every X (week, month, ...)
        """
        if self.book is None:
            raise GncPriceError("Cannot update price for a commodity not attached to a book")

        # get last_price updated
        last_price = self.prices.order_by(-Price.date).limit(1).first()

        if start_date is None:
            start_date = datetime.datetime.today().date() + datetime.timedelta(days=-7)

        if last_price:
            start_date = max(last_price.date.date() + datetime.timedelta(days=1),
                             start_date)

        if self.namespace == "CURRENCY":
            # get reference currency (from book.root_account)
            default_currency = self.base_currency
            if default_currency == self:
                raise GncPriceError("Cannot update exchange rate for base currency")

            # through Quandl for exchange rates
            quotes = quandl_fx(self.mnemonic, default_currency.mnemonic, start_date)
            for q in quotes:
                p = Price(commodity=self,
                          currency=default_currency,
                          date=datetime.datetime.strptime(q.date, "%Y-%m-%d"),
                          value=str(q.rate))

        else:
            symbol = self.mnemonic
            share = yahoo_finance.Share(symbol)
            currency = self.book.currencies(mnemonic=share.data_set["Currency"])

            # get historical data
            for q in share.get_historical("{:%Y-%m-%d}".format(start_date),
                                          "{:%Y-%m-%d}".format(datetime.date.today()),
                                          ):
                day, close = q["Date"], q["Close"]
                Price(commodity=self,
                      currency=currency,
                      date=datetime.datetime.strptime(day, "%Y-%m-%d"),
                      value=Decimal(close),
                      type='last')
