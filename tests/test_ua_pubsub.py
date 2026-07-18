import sys
import json

sys.path.insert(0, "spark-gateway")
import ua_pubsub

import pytest


# --- NetworkMessage / DataSetMessage envelope ------------------------------


def test_build_network_message_state():
    msg = ua_pubsub.build_network_message(
        "arm1", ua_pubsub.DSW_STATE, 42, {"base": 90.0}
    )
    assert msg["MessageType"] == "ua-data"
    assert msg["PublisherId"] == "arm1"
    assert isinstance(msg["MessageId"], str) and msg["MessageId"]
    assert isinstance(msg["Messages"], list) and len(msg["Messages"]) == 1
    ds = msg["Messages"][0]
    assert ds["DataSetWriterId"] == 1
    assert ds["SequenceNumber"] == 42
    assert ds["MetaDataVersion"] == {"MajorVersion": 1, "MinorVersion": 0}
    assert ds["MessageType"] == "ua-keyframe"
    assert isinstance(ds["Timestamp"], str) and ds["Timestamp"]
    # Concrete scalar Double collapses to the bare value (Part 14 Table 186).
    assert ds["Payload"]["base"] == 90.0


def test_status_omitted_when_good():
    # Status shall be omitted from the DataSetMessage when the code is Good (0).
    ds = ua_pubsub.build_network_message(
        "arm1", ua_pubsub.DSW_STATE, 1, {"base": 90.0}
    )["Messages"][0]
    assert "Status" not in ds


def test_status_present_when_bad():
    bad = 0x80340000  # Bad_NoCommunication
    ds = ua_pubsub.build_network_message(
        "arm1", ua_pubsub.DSW_STATE, 1, {"base": 90.0}, status=bad
    )["Messages"][0]
    assert ds["Status"] == {"Code": bad}


def test_bool_field_is_bare_value():
    ds = ua_pubsub.build_network_message(
        "arm1", ua_pubsub.DSW_STATE, 1, {"safe_stop": True}
    )["Messages"][0]
    assert ds["Payload"]["safe_stop"] is True


# --- DataValue field encoding (DataSetFieldContentMask) --------------------


def test_datavalue_field_encoding():
    mask = ua_pubsub.FIELD_SOURCE_TIMESTAMP
    ds = ua_pubsub.build_network_message(
        "arm1", ua_pubsub.DSW_STATE, 1, {"base": 90.0}, field_content_mask=mask
    )["Messages"][0]
    field = ds["Payload"]["base"]
    assert field["Value"] == 90.0
    assert isinstance(field["SourceTimestamp"], str) and field["SourceTimestamp"]
    # Concrete DataType => no UaType.
    assert "UaType" not in field


# --- Keep-alive ------------------------------------------------------------


def test_keepalive_has_no_payload():
    ds = ua_pubsub.build_network_message(
        "arm1", ua_pubsub.DSW_STATE, 7, {}, message_type=ua_pubsub.DS_KEEPALIVE
    )["Messages"][0]
    assert "Payload" not in ds
    assert ds["MessageType"] == "ua-keepalive"


# --- encode / decode round trips -------------------------------------------


def test_encode_valid_json():
    s = ua_pubsub.encode("arm1", ua_pubsub.DSW_STATE, 7, {"base": 90.0})
    assert isinstance(s, str)
    json.loads(s)  # raises if invalid


def test_round_trip_state():
    decoded = ua_pubsub.decode(
        ua_pubsub.encode(
            "arm1", ua_pubsub.DSW_STATE, 7, {"base": 90.0, "pitch": 45.5}
        )
    )
    assert decoded["publisher_id"] == "arm1"
    assert decoded["message_type"] == "ua-data"
    assert len(decoded["datasets"]) == 1
    ds = decoded["datasets"][0]
    assert ds["writer_id"] == 1
    assert ds["sequence_number"] == 7
    assert ds["message_type"] == "ua-keyframe"
    assert ds["status"] == 0
    assert ds["fields"] == {"base": 90.0, "pitch": 45.5}


def test_round_trip_datavalue_encoding():
    decoded = ua_pubsub.decode(
        ua_pubsub.encode(
            "arm1",
            ua_pubsub.DSW_STATE,
            1,
            {"base": 90.0, "safe_stop": False},
            field_content_mask=ua_pubsub.FIELD_SOURCE_TIMESTAMP,
        )
    )
    ds = decoded["datasets"][0]
    assert ds["fields"] == {"base": 90.0, "safe_stop": False}


def test_command_round_trip():
    dec = ua_pubsub.decode(
        ua_pubsub.encode("fleet-gateway", ua_pubsub.DSW_COMMAND, 3, {"base": 120.0})
    )
    assert len(dec["datasets"]) == 1
    ds = dec["datasets"][0]
    assert ds["writer_id"] == 2
    assert ds["fields"] == {"base": 120.0}


# --- Decoder tolerance -----------------------------------------------------


def test_decode_variant_with_uatype():
    # Explicit Variant object form: {"UaType": .., "Value": ..}.
    payload = {
        "MessageType": "ua-data",
        "PublisherId": "arm1",
        "Messages": [
            {
                "DataSetWriterId": 1,
                "SequenceNumber": 1,
                "Payload": {"base": {"UaType": 11, "Value": 90.0}},
            }
        ],
    }
    ds = ua_pubsub.decode(json.dumps(payload))["datasets"][0]
    assert ds["fields"]["base"] == 90.0


def test_decode_legacy_1_04_variant():
    # Backwards compatibility with the deprecated {"Value": {"Type": .., "Body": ..}}.
    payload = {
        "MessageType": "ua-data",
        "PublisherId": "arm1",
        "Messages": [
            {
                "DataSetWriterId": 1,
                "SequenceNumber": 1,
                "Payload": {"base": {"Value": {"Type": 11, "Body": 90.0}}},
            }
        ],
    }
    ds = ua_pubsub.decode(json.dumps(payload))["datasets"][0]
    assert ds["fields"]["base"] == 90.0


def test_decode_status_object():
    payload = {
        "MessageType": "ua-data",
        "PublisherId": "arm1",
        "Messages": [
            {
                "DataSetWriterId": 1,
                "SequenceNumber": 1,
                "Status": {"Code": 0x80340000, "Symbol": "Bad_NoCommunication"},
                "Payload": {"base": 90.0},
            }
        ],
    }
    ds = ua_pubsub.decode(json.dumps(payload))["datasets"][0]
    assert ds["status"] == 0x80340000


def test_decode_headerless_array():
    # NetworkMessage header disabled => bare array of DataSetMessages.
    raw = ua_pubsub.encode(
        "arm1",
        ua_pubsub.DSW_STATE,
        5,
        {"base": 90.0},
        nm_content_mask=ua_pubsub.NM_DATASET_MESSAGE_HEADER,
    )
    assert json.loads(raw).__class__ is list
    dec = ua_pubsub.decode(raw)
    assert dec["datasets"][0]["fields"] == {"base": 90.0}


def test_decode_invalid_json():
    with pytest.raises(ValueError):
        ua_pubsub.decode("{not json")


def test_decode_non_ua_data():
    bad = json.dumps(
        {"MessageType": "ua-metadata", "PublisherId": "x", "Messages": []}
    )
    with pytest.raises(ValueError):
        ua_pubsub.decode(bad)
