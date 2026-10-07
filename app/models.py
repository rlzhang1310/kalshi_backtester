import re
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, field_validator


TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,199}$")
TERMINAL = {"filled", "canceled", "expired", "rejected"}


def decimal(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid decimal") from exc
    if not number.is_finite():
        raise ValueError("Decimal must be finite")
    return number


def canonical(value: Decimal) -> str:
    return format(value.normalize(), "f")


def ticker(value: str) -> str:
    result = value.strip().upper()
    if not TICKER.fullmatch(result):
        raise ValueError("Invalid exact market ticker")
    return result


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    ticker: str
    outcome: Literal["yes", "no"]
    price: str
    mode: Literal["maker"]

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, value: str) -> str:
        return ticker(value)

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Price must be a decimal string")
        number = decimal(value)
        if not 0 < number < 1 or number.as_tuple().exponent < -4:
            raise ValueError("Price must be between 0 and 1 with at most four decimal places")
        return canonical(number)


class PairedOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pair_id: UUID
    primary_ticker: str
    primary_outcome: Literal["yes", "no"]
    primary_price: str
    second_target: Literal["other_market", "same_market"]
    other_ticker: str | None = None
    other_outcome: Literal["yes", "no"] | None = None
    relationship: Literal["same_outcome", "opposite_outcomes"] | None = None
    second_price: str
    mode: Literal["maker"]

    @field_validator("primary_ticker")
    @classmethod
    def validate_primary_ticker(cls, value: str) -> str:
        return ticker(value)

    @field_validator("other_ticker")
    @classmethod
    def validate_other_ticker(cls, value: str | None) -> str | None:
        return ticker(value) if value is not None else None

    @field_validator("primary_price", "second_price")
    @classmethod
    def validate_pair_price(cls, value: str) -> str:
        return OrderRequest.validate_price(value)

    def legs(self) -> tuple[OrderRequest, OrderRequest]:
        if self.second_target == "other_market":
            if not self.other_ticker or not self.other_outcome or not self.relationship:
                raise ValueError("Lock an exact other-market relationship before posting")
            if self.other_ticker == self.primary_ticker:
                raise ValueError("Use same-market target when both legs use one ticker")
            second_ticker = self.other_ticker
            second_outcome = ("no" if self.other_outcome == "yes" else "yes") if self.relationship == "same_outcome" else self.other_outcome
        else:
            if any(value is not None for value in (self.other_ticker, self.other_outcome, self.relationship)):
                raise ValueError("Same-market pair must not specify an other-market relationship")
            second_ticker = self.primary_ticker
            second_outcome = "no" if self.primary_outcome == "yes" else "yes"
        first = OrderRequest(request_id=uuid5(self.pair_id, "leg-0"),
                             ticker=self.primary_ticker, outcome=self.primary_outcome,
                             price=self.primary_price, mode=self.mode)
        second = OrderRequest(request_id=uuid5(self.pair_id, "leg-1"),
                              ticker=second_ticker, outcome=second_outcome,
                              price=self.second_price, mode=self.mode)
        return first, second
