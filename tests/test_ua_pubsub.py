import sys
import json

sys.path.insert(0, "spark-gateway")
import ua_pubsub

import pytest


def test_build_network_message_state():
    msg = ua_pubsub.build_network_message("arm1", ua_pubsub.DSW_STATE, 42, {"base": 90.0})
    assert msg["MessageType"] == "ua-data"
    assert msg["PublisherId"] == "arm1"
    assert isinstance(msg["Messages"], list) and len(msg["Messages"]) == 1
    ds = msg["Messages"][0]
    assert ds["DataSetWriterId"] == 1
    assert ds["SequenceNumber"] == 42
    assert ds["MetaDataVersion"] == {"MajorVersion": 1, "MinorVersion": 0}
    assert ds["Status"] == 0
    assert isinstance(ds["Timestamp"], str) and ds["Timestamp"]
    assert ds["Payload"]["base"] == {"Value": {"Type": 11, "Body": 90.0}}


def test_build_network_message_bool():
    msg = ua_pubsub.build_network_message("arm1", ua_pubsub.DSW_STATE, 1, {"safe_stop": True})
    assert msg["Messages"][0]["Payload"]["safe_stop"] == {"Value": {"Type": 1, "Body": True}}


def test_encode_valid_json():
    s = ua_pubsub.encode("arm1", ua_pubsub.DSW_STATE, 7, {"base": 90.0})
    assert isinstance(s, str)
    json.loads(s)  # raises if invalid


def test_round_trip_state():
    decoded = ua_pubsub.decode(ua_pubsub.encode(
        "arm1", ua_pubsub.DSW_STATE, 7, {"base": 90.0, "pitch": 45.5}))
    assert decoded["publisher_id"] == "arm1"
    assert decoded["message_type"] == "ua-data"
    assert len(decoded["datasets"]) == 1
    ds = decoded["datasets"][0]
    assert ds["writer_id"] == 1
    assert ds["sequence_number"] == 7
    assert ds["fields"] == {"base": 90.0, "pitch": 45.5}


def test_decode_invalid_json():
    with pytest.raises(ValueError):
        ua_pubsub.decode("{not json")


def test_decode_non_ua_data():
    bad = json.dumps({"MessageType": "ua-metadata", "PublisherId": "x", "Messages": []})
    with pytest.raises(ValueError):
        ua_pubsub.decode(bad)


def test_command_round_trip():
    dec = ua_pubsub.decode(ua_pubsub.encode(
        "fleet-gateway", ua_pubsub.DSW_COMMAND, 3, {"base": 120.0}))
    assert len(dec["datasets"]) == 1
    ds = dec["datasets"][0]
    assert ds["writer_id"] == 2
    assert ds["fields"] == {"base": 120.0}
