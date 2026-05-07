from __future__ import annotations

from .offer_record_reads import OfferRecordReadStoreMixin
from .offer_record_writes import OfferRecordWriteStoreMixin


class OfferRecordStoreMixin(
    OfferRecordReadStoreMixin,
    OfferRecordWriteStoreMixin,
):
    pass
