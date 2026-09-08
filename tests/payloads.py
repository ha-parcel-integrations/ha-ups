"""Sample UPS ``trackDetails[]`` entries shared by the test modules."""
from __future__ import annotations

ACTIVE_CODE = "1Z999AA10123456784"
DELIVERED_CODE = "1Z999AA10123456999"


def _activity(
    act_code: str,
    name_key: str | None,
    gmt_date: str,
    gmt_time: str,
) -> dict:
    """One ``shipmentProgressActivities[]`` entry."""
    return {
        "actCode": act_code,
        "gmtDate": gmt_date,
        "gmtTime": gmt_time,
        "gmtOffset": "+00:00",
        "milestoneName": {"nameKey": name_key} if name_key else None,
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative ``trackDetails[]`` entry for a delivered parcel.

    Newest-first, matching the API's own order — the delivered scan is
    index 0, the label-created scan is last.
    """
    return {
        "requestedTrackingNumber": code,
        "trackingNumber": code,
        "errorCode": None,
        "errorText": None,
        "packageStatus": "Delivered",
        "packageStatusType": "D",
        "packageStatusCode": "011",
        "isDelivered": True,
        "isDeliveredToUAP": False,
        "isPickedUpByCustomer": False,
        "currentMilestone": {"nameKey": "cms.stapp.delivered"},
        "milestones": [
            {"nameKey": "cms.stapp.orderReceived", "isCurrent": False},
            {"nameKey": "cms.stapp.weHaveYourPkg", "isCurrent": False},
            {"nameKey": "cms.stapp.inTransit", "isCurrent": False},
            {"nameKey": "cms.stapp.outForDelivery", "isCurrent": False},
            {"nameKey": "cms.stapp.delivered", "isCurrent": True},
        ],
        "shipmentProgressActivities": [
            _activity("FS", "cms.stapp.delivered", "20260429", "13:12:42"),
            _activity("OT", "cms.stapp.outForDelivery", "20260429", "08:46:00"),
            _activity("AR", "cms.stapp.inTransit", "20260428", "15:52:17"),
            _activity("DP", None, "20260428", "06:03:11"),
            _activity("OR", "cms.stapp.weHaveYourPkg", "20260427", "23:03:58"),
            _activity("MP", "cms.stapp.orderReceived", "20260427", "10:00:00"),
        ],
        "upsAccessPoint": None,
        "additionalInformation": {
            "serviceInformation": {"serviceName": "UPS Standard&#174;"},
            "weight": "",
            "weightUnit": None,
        },
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel — the ladder short of the delivered scan."""
    sample = delivered_sample(code)
    sample.update(
        {
            "packageStatus": "Out For Delivery",
            "packageStatusType": "I",
            "packageStatusCode": "OT",
            "isDelivered": False,
            "currentMilestone": {"nameKey": "cms.stapp.outForDelivery"},
            "shipmentProgressActivities": sample["shipmentProgressActivities"][1:],
        }
    )
    sample["milestones"] = [
        {**m, "isCurrent": m["nameKey"] == "cms.stapp.outForDelivery"}
        for m in sample["milestones"]
    ]
    return sample


def weighed_sample(code: str = DELIVERED_CODE) -> dict:
    """A delivered parcel with a populated weight — never seen live yet, but
    the mapping (KGS/LBS -> kg) must still be exercised."""
    sample = delivered_sample(code)
    sample["additionalInformation"]["weight"] = "1.25"
    sample["additionalInformation"]["weightUnit"] = "KGS"
    return sample


def not_found_envelope(code: str) -> dict:
    """The ``trackDetails[0]`` entry for an unknown tracking number."""
    return {
        "requestedTrackingNumber": code,
        "trackingNumber": code,
        "errorCode": "504",
        "errorText": "Tracking number not found in database",
    }
