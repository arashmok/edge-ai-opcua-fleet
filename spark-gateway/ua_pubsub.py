"""
OPC UA PubSub JSON message-mapping codec.

Implements the JSON Message Mapping of OPC UA Part 14 (OPC 10000-14) section
7.2.5, using the OPC UA JSON Data Encoding of Part 6 (OPC 10000-6, v1.05,
Compact / Verbose encoding).

Scope
-----
This module is the *message-mapping* layer only: it (de)serialises the
NetworkMessage / DataSetMessage envelope that a Publisher and a Subscriber
exchange over a Message Oriented Middleware (here MQTT, Part 14 section 7.3.4).
It deliberately is NOT a full PubSub configuration model -- WriterGroups,
ReaderGroups, the Security Key Service, discovery messages (ua-metadata,
ua-status, ...) and the UADP binary mapping are out of scope and belong to the
Publisher / Subscriber applications (pubsub_agent.py on the robots, gateway.py
on the Spark).

NetworkMessage layout (Part 14 Table 184)
-----------------------------------------
{
  "MessageId":      "<guid>",              # always present
  "MessageType":    "ua-data",             # always present
  "PublisherId":    "<publisher>",         # JsonNetworkMessageContentMask.PublisherId
  "DataSetClassId": "<guid>",              # optional
  "Messages": [ <DataSetMessage>, ... ]    # array (object if SingleDataSetMessage)
}

DataSetMessage layout (Part 14 Table 185)
-----------------------------------------
{
  "DataSetWriterId": 1,                    # UInt16
  "SequenceNumber":  0,                    # UInt32, starts at 0, +1 per msg (7.2.3)
  "MetaDataVersion": {"MajorVersion": 1, "MinorVersion": 0},
  "Timestamp":       "2024-01-01T00:00:00+00:00",
  "Status":          {"Code": N},          # OMITTED when Good (Code 0)
  "MessageType":     "ua-keyframe",        # ua-keyframe|ua-deltaframe|ua-event|ua-keepalive
  "Payload": { "<field>": <encoded field>, ... }   # absent for ua-keepalive
}

DataSet field encoding (Part 14 section 7.2.5.4, Tables 186/187)
----------------------------------------------------------------
Controlled by the DataSetFieldContentMask:

* mask == 0x00 or 0x20 (RawData) -> Variant / VerboseEncoding.
  A scalar with a concrete DataType collapses to the bare JSON value:
      "base": 90.0
  Only abstract types (BaseDataType / Variant) carry an explicit "UaType":
      "any": {"UaType": 12, "Value": "Apple"}

* mask with any DataValue bit set -> DataValue / VerboseEncoding:
      "base": {"Value": 90.0, "SourceTimestamp": "..."}
  "Status" is omitted when the field status is Good; "UaType" is omitted for
  concrete DataTypes.

The bare-value Variant encoding (mask 0x00) is the default here: it is the
leanest fully-compliant form and is what standards-compliant JSON subscribers
(open62541, Siemens, Prosys, ...) expect for concrete scalar DataTypes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


# --- OPC UA BuiltInType identifiers (Part 6, Table 1) ----------------------
BUILTIN_BOOLEAN = 1
BUILTIN_SBYTE = 2
BUILTIN_BYTE = 3
BUILTIN_INT16 = 4
BUILTIN_UINT16 = 5
BUILTIN_INT32 = 6
BUILTIN_UINT32 = 7
BUILTIN_INT64 = 8
BUILTIN_UINT64 = 9
BUILTIN_FLOAT = 10
BUILTIN_DOUBLE = 11
BUILTIN_STRING = 12
BUILTIN_DATETIME = 13

# Abstract builtin types must always carry a "UaType" so a Subscriber can
# recover the concrete DataType (Part 14 section 7.2.5.4.3). Concrete scalar
# types collapse to the bare value under VerboseEncoding.
BUILTIN_VARIANT = 24
_ABSTRACT_BUILTINS = frozenset({0, BUILTIN_VARIANT})  # 0 == BaseDataType

# --- MessageType strings (Part 14 Table 183 / section 7.2.5.4.1) -----------
MSG_DATA = "ua-data"
DS_KEYFRAME = "ua-keyframe"
DS_DELTAFRAME = "ua-deltaframe"
DS_EVENT = "ua-event"
DS_KEEPALIVE = "ua-keepalive"
_VALID_DS_MESSAGE_TYPES = frozenset(
    {DS_KEYFRAME, DS_DELTAFRAME, DS_EVENT, DS_KEEPALIVE}
)

# --- JsonNetworkMessageContentMask (Part 14 section 6.3.2.1.1) -------------
NM_NETWORK_MESSAGE_HEADER = 0x01
NM_DATASET_MESSAGE_HEADER = 0x02
NM_SINGLE_DATASET_MESSAGE = 0x04
NM_PUBLISHER_ID = 0x08
NM_DATASET_CLASS_ID = 0x10
NM_REPLY_TO = 0x20

# --- JsonDataSetMessageContentMask (Part 14 section 6.3.2.3.1) -------------
DSM_DATASET_WRITER_ID = 0x01
DSM_METADATA_VERSION = 0x02
DSM_SEQUENCE_NUMBER = 0x04
DSM_TIMESTAMP = 0x08
DSM_STATUS = 0x10
DSM_MESSAGE_TYPE = 0x20
DSM_DATASET_WRITER_NAME = 0x40

# --- DataSetFieldContentMask (Part 14 section 6.2.4.2) ---------------------
FIELD_STATUS_CODE = 0x01
FIELD_SOURCE_TIMESTAMP = 0x02
FIELD_SERVER_TIMESTAMP = 0x04
FIELD_SOURCE_PICOSECONDS = 0x08
FIELD_SERVER_PICOSECONDS = 0x10
FIELD_RAW_DATA = 0x20
# Any of these bits selects DataValue field encoding (Part 14 section 7.2.5.4.1).
_FIELD_DATAVALUE_BITS = (
    FIELD_STATUS_CODE
    | FIELD_SOURCE_TIMESTAMP
    | FIELD_SERVER_TIMESTAMP
    | FIELD_SOURCE_PICOSECONDS
    | FIELD_SERVER_PICOSECONDS
)

# --- StatusCode ------------------------------------------------------------
STATUS_GOOD = 0

# --- Application defaults --------------------------------------------------
DSW_STATE = 1     # robots publish joint state with this DataSetWriterId
DSW_COMMAND = 2   # gateway publishes joint commands with this DataSetWriterId

# Default masks: full NetworkMessage + DataSetMessage headers carrying
# PublisherId, and bare Variant/VerboseEncoding field values.
DEFAULT_NM_CONTENT_MASK = (
    NM_NETWORK_MESSAGE_HEADER | NM_DATASET_MESSAGE_HEADER | NM_PUBLISHER_ID
)
DEFAULT_DSM_CONTENT_MASK = (
    DSM_DATASET_WRITER_ID
    | DSM_METADATA_VERSION
    | DSM_SEQUENCE_NUMBER
    | DSM_TIMESTAMP
    | DSM_STATUS
    | DSM_MESSAGE_TYPE
)
DEFAULT_FIELD_CONTENT_MASK = 0x00


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _infer_type(value: Any) -> int:
    """Infer an OPC UA BuiltInType id from a Python value."""
    if isinstance(value, bool):
        return BUILTIN_BOOLEAN
    if isinstance(value, (int, float)):
        # Fleet payloads are joint angles / setpoints: encode as Double.
        return BUILTIN_DOUBLE
    if isinstance(value, str):
        return BUILTIN_STRING
    return BUILTIN_DOUBLE


def encode_variant(value: Any, type_id: int, *, include_type: bool = False) -> Any:
    """
    Encode a Variant using the OPC UA JSON Data Encoding (Part 6).

    Under VerboseEncoding a scalar with a concrete DataType collapses to the
    bare value; abstract types must include "UaType". Set include_type=True to
    force the explicit ``{"UaType": id, "Value": value}`` object form.
    """
    if include_type or type_id in _ABSTRACT_BUILTINS:
        return {"UaType": type_id, "Value": value}
    return value


def encode_field(
    value: Any,
    type_id: int,
    field_content_mask: int,
    timestamp: str,
) -> Any:
    """Encode a single DataSet field per the DataSetFieldContentMask."""
    abstract = type_id in _ABSTRACT_BUILTINS
    if field_content_mask & _FIELD_DATAVALUE_BITS:
        # DataValue / VerboseEncoding (Part 14 Table 187).
        data_value: dict = {}
        if abstract:
            data_value["UaType"] = type_id
        data_value["Value"] = value
        if field_content_mask & FIELD_SOURCE_TIMESTAMP:
            data_value["SourceTimestamp"] = timestamp
        if field_content_mask & FIELD_SERVER_TIMESTAMP:
            data_value["ServerTimestamp"] = timestamp
        # Status is only present for a non-Good field status; the fleet
        # payloads carry Good values, so it is omitted (Part 14 section 7.2.5.4.2).
        return data_value
    # Variant / VerboseEncoding (Part 14 Table 186).
    return encode_variant(value, type_id, include_type=abstract)


# Backwards-compatible aliases (older helper names).
def variant(value: Any, type_id: int, *, include_type: bool = True) -> Any:
    return encode_variant(value, type_id, include_type=include_type)


def data_value(
    value: Any,
    type_id: int | None = None,
    *,
    source_timestamp: str | None = None,
    status: int = STATUS_GOOD,
) -> dict:
    dv: dict = {}
    if type_id is not None and type_id in _ABSTRACT_BUILTINS:
        dv["UaType"] = type_id
    dv["Value"] = value
    if source_timestamp is not None:
        dv["SourceTimestamp"] = source_timestamp
    if status != STATUS_GOOD:
        dv["Status"] = {"Code": status}
    return dv


def build_dataset_message(
    dataset_writer_id: int,
    sequence_number: int,
    payload_fields: dict,
    *,
    field_types: dict | None = None,
    message_type: str = DS_KEYFRAME,
    dsm_content_mask: int = DEFAULT_DSM_CONTENT_MASK,
    field_content_mask: int = DEFAULT_FIELD_CONTENT_MASK,
    status: int = STATUS_GOOD,
    timestamp: str | None = None,
    metadata_version: dict | None = None,
) -> dict:
    """Build a single JSON DataSetMessage (Part 14 Table 185)."""
    if message_type not in _VALID_DS_MESSAGE_TYPES:
        raise ValueError(f"Invalid DataSetMessage MessageType: {message_type!r}")
    timestamp = timestamp or _utc_now_iso()
    field_types = field_types or {}

    ds: dict = {}
    if dsm_content_mask & DSM_DATASET_WRITER_ID:
        ds["DataSetWriterId"] = dataset_writer_id
    if dsm_content_mask & DSM_SEQUENCE_NUMBER:
        ds["SequenceNumber"] = sequence_number
    if dsm_content_mask & DSM_METADATA_VERSION:
        ds["MetaDataVersion"] = metadata_version or {
            "MajorVersion": 1,
            "MinorVersion": 0,
        }
    if dsm_content_mask & DSM_TIMESTAMP:
        ds["Timestamp"] = timestamp
    # Status is omitted when Good (Part 14 section 7.2.5.4.2).
    if (dsm_content_mask & DSM_STATUS) and status != STATUS_GOOD:
        ds["Status"] = {"Code": status}
    if dsm_content_mask & DSM_MESSAGE_TYPE:
        ds["MessageType"] = message_type

    # A keep-alive DataSetMessage carries no Payload (Part 14 Table 185).
    if message_type != DS_KEEPALIVE:
        payload = {}
        for name, val in payload_fields.items():
            type_id = field_types.get(name, _infer_type(val))
            payload[name] = encode_field(
                val, type_id, field_content_mask, timestamp
            )
        ds["Payload"] = payload
    return ds


def build_network_message(
    publisher_id: str,
    dataset_writer_id: int,
    sequence_number: int,
    payload_fields: dict,
    *,
    field_types: dict | None = None,
    message_type: str = DS_KEYFRAME,
    nm_content_mask: int = DEFAULT_NM_CONTENT_MASK,
    dsm_content_mask: int = DEFAULT_DSM_CONTENT_MASK,
    field_content_mask: int = DEFAULT_FIELD_CONTENT_MASK,
    status: int = STATUS_GOOD,
    dataset_class_id: str | None = None,
    timestamp: str | None = None,
    message_id: str | None = None,
) -> Any:
    """
    Build a complete JSON NetworkMessage (Part 14 Table 184).

    Returns a dict when the NetworkMessage header is present. If the header is
    disabled the return is the bare Messages content: an array of
    DataSetMessages, or a single DataSetMessage object when SingleDataSetMessage
    is set (Part 14 section 7.2.5.3).
    """
    ds = build_dataset_message(
        dataset_writer_id,
        sequence_number,
        payload_fields,
        field_types=field_types,
        message_type=message_type,
        dsm_content_mask=dsm_content_mask,
        field_content_mask=field_content_mask,
        status=status,
        timestamp=timestamp,
    )

    if not (nm_content_mask & NM_NETWORK_MESSAGE_HEADER):
        # Header-less NetworkMessage is the Messages content itself.
        return ds if (nm_content_mask & NM_SINGLE_DATASET_MESSAGE) else [ds]

    nm: dict = {
        "MessageId": message_id or str(uuid.uuid4()),
        "MessageType": MSG_DATA,
    }
    if nm_content_mask & NM_PUBLISHER_ID:
        nm["PublisherId"] = publisher_id
    if (nm_content_mask & NM_DATASET_CLASS_ID) and dataset_class_id is not None:
        nm["DataSetClassId"] = dataset_class_id
    if nm_content_mask & NM_SINGLE_DATASET_MESSAGE:
        nm["Messages"] = ds
    else:
        nm["Messages"] = [ds]
    return nm


def encode(
    publisher_id: str,
    dataset_writer_id: int,
    sequence_number: int,
    payload_fields: dict,
    field_types: dict | None = None,
    *,
    message_type: str = DS_KEYFRAME,
    nm_content_mask: int = DEFAULT_NM_CONTENT_MASK,
    dsm_content_mask: int = DEFAULT_DSM_CONTENT_MASK,
    field_content_mask: int = DEFAULT_FIELD_CONTENT_MASK,
    status: int = STATUS_GOOD,
    dataset_class_id: str | None = None,
) -> str:
    """Encode a NetworkMessage to a JSON string."""
    return json.dumps(
        build_network_message(
            publisher_id,
            dataset_writer_id,
            sequence_number,
            payload_fields,
            field_types=field_types,
            message_type=message_type,
            nm_content_mask=nm_content_mask,
            dsm_content_mask=dsm_content_mask,
            field_content_mask=field_content_mask,
            status=status,
            dataset_class_id=dataset_class_id,
        )
    )


def _decode_status(raw: Any) -> int:
    """Normalise a JSON StatusCode (int or {"Code": N}) to an int code."""
    if isinstance(raw, dict):
        return int(raw.get("Code", 0))
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    return 0


def _decode_field(raw: Any) -> Any:
    """
    Unwrap a DataSet field value to its Python scalar, tolerating every
    encoding a compliant Publisher may emit:

      * bare value                         -> value                 (Variant verbose)
      * {"UaType": t, "Value": v}          -> v                     (Variant)
      * {"Value": v, "SourceTimestamp": .} -> v                     (DataValue verbose)
      * {"Value": {"Type": t, "Body": v}}  -> v                     (legacy 1.04 form)
      * {"Type": t, "Body": v}             -> v                     (legacy bare Variant)
    """
    if isinstance(raw, dict):
        if "Value" in raw:
            inner = raw["Value"]
            if isinstance(inner, dict) and "Body" in inner and "Type" in inner:
                return inner["Body"]  # legacy DataValue wrapping a 1.04 Variant
            return inner  # DataValue (verbose) or Variant with UaType
        if "Body" in raw and "Type" in raw:
            return raw["Body"]  # legacy bare Variant
    return raw


def _decode_dataset(ds: Any) -> dict:
    if not isinstance(ds, dict):
        raise ValueError("Each DataSetMessage must be a JSON object")
    payload = ds.get("Payload", {})
    if payload is None:  # keep-alive
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object")
    return {
        "writer_id": ds.get("DataSetWriterId"),
        "sequence_number": ds.get("SequenceNumber"),
        "timestamp": ds.get("Timestamp", ""),
        "status": _decode_status(ds.get("Status", STATUS_GOOD)),
        "message_type": ds.get("MessageType", DS_KEYFRAME),
        "fields": {name: _decode_field(v) for name, v in payload.items()},
    }


def decode(raw: bytes | str) -> dict:
    """
    Decode a JSON NetworkMessage into a normalised dict::

        {
            "publisher_id": str | None,
            "message_type": "ua-data",
            "datasets": [
                {
                    "writer_id": int,
                    "sequence_number": int,
                    "timestamp": str,
                    "status": int,
                    "message_type": str,
                    "fields": {field_name: python_value},
                },
                ...
            ],
        }

    Accepts the full NetworkMessage form as well as the header-less variants
    (array of DataSetMessages, or a single DataSetMessage object) defined in
    Part 14 section 7.2.5.3.

    Raises ValueError on malformed JSON or a non "ua-data" NetworkMessage.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    publisher_id: str | None = None
    message_type = MSG_DATA

    if isinstance(msg, list):
        raw_datasets = msg  # header-less array of DataSetMessages
    elif isinstance(msg, dict):
        if "MessageType" in msg:
            message_type = msg.get("MessageType")
            if message_type != MSG_DATA:
                raise ValueError(
                    f"Expected MessageType 'ua-data', got {message_type!r}"
                )
            publisher_id = msg.get("PublisherId")
            messages = msg.get("Messages")
            if isinstance(messages, list):
                raw_datasets = messages
            elif isinstance(messages, dict):
                raw_datasets = [messages]  # SingleDataSetMessage
            else:
                raise ValueError("Messages must be a JSON array or object")
        elif "Payload" in msg or "DataSetWriterId" in msg:
            raw_datasets = [msg]  # header-less single DataSetMessage
        else:
            raise ValueError("Unrecognised NetworkMessage structure")
    else:
        raise ValueError("Message must be a JSON object or array")

    datasets = [_decode_dataset(ds) for ds in raw_datasets]

    # PublisherId may be carried on the DataSetMessage when the NetworkMessage
    # header is absent (Part 14 Table 185).
    if publisher_id is None:
        for ds in raw_datasets:
            if isinstance(ds, dict) and isinstance(ds.get("PublisherId"), str):
                publisher_id = ds["PublisherId"]
                break

    return {
        "publisher_id": publisher_id,
        "message_type": message_type,
        "datasets": datasets,
    }
