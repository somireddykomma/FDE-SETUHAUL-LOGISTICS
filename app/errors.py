class ServiceError(Exception):
    """Base class for errors the API layer should turn into a clean response."""


class ShipmentNotFound(ServiceError):
    pass


class ShipmentAmbiguous(ServiceError):
    def __init__(self, driver_id: str, shipment_ids: list[str]):
        self.driver_id = driver_id
        self.shipment_ids = shipment_ids
        super().__init__(
            f"driver {driver_id} has {len(shipment_ids)} active shipments: {shipment_ids}"
        )


class SlotNotFound(ServiceError):
    pass


class SlotUnavailable(ServiceError):
    """Slot is not AVAILABLE: occupied, blocked/closed, or held by another thread."""


class HoldNotFound(ServiceError):
    pass


class HoldNotActive(ServiceError):
    """Hold exists but has expired, was released, or already converted."""


class HoldMismatch(ServiceError):
    """Hold exists but does not belong to the shipment/slot being requested."""


class AppointmentConflict(ServiceError):
    """Another appointment/hold won the race for this slot (DB unique index fired)."""


class ThreadNotFound(ServiceError):
    pass


class ImplausibleEta(ServiceError):
    """declared_eta_ts is too far from the shipment's known timeline to be a
    real delay estimate -- almost always a date-arithmetic slip (wrong month/
    day/year) rather than an actual multi-week delay."""
