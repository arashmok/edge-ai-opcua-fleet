import sys, json
sys.path.insert(0, "spark-gateway")
import ua_pubsub as u

def test_build_structure():
    m = u.build_network_message("arm1", u.DSW_STATE, 42, {"base": 90.0})
    assert m["MessageType"] == "ua-data"
    assert isinstance(m["MessageId"], str) and m["MessageId"]
    assert m["PublisherId"] == "arm1"
    assert len(m["Messages"]) == 1
    d = m["Messages"][0]
    assert d["DataSetWriterId"] == 1
    assert d["SequenceNumber"] == 42
    assert d["MetaDataVersion"] == {"MajorVersion": 1, "MinorVersion": 0}
    assert d["Status"] == 0
    assert isinstance(d["Timestamp"], str) and d["Timestamp"]
    assert d["Payload"]["base"] == {"Value": {"Type": 11, "Body": 90.0}}

def test_bool_type():
    m = u.build_network_message("arm1", u.DSW_STATE, 1, {"safe_stop": True})
    assert m["Messages"][0]["Payload"]["safe_stop"] == {"Value": {"Type": 1, "Body": True}}

def test_encode_json():
    assert json.loads(u.encode("arm1", u.DSW_STATE, 7, {"base": 90.0}))["MessageType"] == "ua-data"

def test_state_roundtrip():
    r = u.decode(u.encode("arm1", u.DSW_STATE, 7, {"base": 90.0, "pitch": 45.5}))
    assert r["publisher_id"] == "arm1"
    assert r["message_type"] == "ua-data"
    assert len(r["datasets"]) == 1
    assert r["datasets"][0]["writer_id"] == 1
    assert r["datasets"][0]["sequence_number"] == 7
    assert r["datasets"][0]["fields"] == {"base": 90.0, "pitch": 45.5}

def test_decode_errors():
    import pytest
    with pytest.raises(ValueError):
        u.decode("{not json")
    with pytest.raises(ValueError):
        u.decode(json.dumps({"MessageType": "ua-metadata", "PublisherId": "x", "Messages": []}))

def test_bare_value():
    bare = json.dumps({"MessageType": "ua-data", "PublisherId": "arm1",
                       "Messages": [{"DataSetWriterId": 1, "SequenceNumber": 1, "Payload": {"base": 90.0}}]})
    assert u.decode(bare)["datasets"][0]["fields"] == {"base": 90.0}

def test_command_roundtrip():
    rc = u.decode(u.encode("fleet-gateway", u.DSW_COMMAND, 3, {"base": 120.0}))
    assert rc["datasets"][0]["writer_id"] == 2
    assert rc["datasets"][0]["fields"] == {"base": 120.0}
