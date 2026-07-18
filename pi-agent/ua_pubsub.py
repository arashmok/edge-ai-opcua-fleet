"""
OPC UA PubSub JSON NetworkMessage codec (OPC UA Part 14, JSON Message Mapping).

This module provides encoding/decoding of OPC UA PubSub JSON NetworkMessages
as specified in OPC UA Part 14, section "JSON Message Mapping".

Topic convention:
  - State messages: fleet/<robot_id>/state
  - Command messages: fleet/<robot_id>/cmd

NetworkMessage structure per spec:
  {
    "MessageId": "uuid-string",
    "MessageType": "ua-data",
    "PublisherId": "fleet-gateway" | "<robot_id>",
    "Messages": [
      {
        "DataSetWriterId": 1 | 2,
        "SequenceNumber": 1,
        "MetaDataVersion": {"MajorVersion": 1, "MinorVersion": 0},
        "Timestamp": "2024-01-01T00:00:00+00:00",
        "Status": 0,
        "MessageType": "ua-keyframe",
        "Payload": {
          "joint_name": {"Value": {"Type": 11, "Body": 90.0}},
          ...
        }
      }
    ]
  }

Builtin types:
  - Double (11): OPC UA Double (64-bit float)
  - Boolean (1): OPC UA Boolean
"""

import json
import uuid
from datetime import timezone, datetime
from typing import Any


BUILTIN_DOUBLE = 11
BUILTIN_BOOLEAN = 1

DSW_STATE = 1     # Robots publish joint state with this DataSetWriterId
DSW_COMMAND = 2   # Gateway publishes joint commands with this DataSetWriterId


def variant(value: Any, type_id: int) -> dict:
    """Build a Variant object for a DataValue."""
    return {"Type": type_id, "Body": value}


def data_value(value: Any, type_id: int) -> dict:
    """Build a DataValue object containing a Variant."""
    return {"Value": variant(value, type_id)}


def _infer_type(value: Any) -> int:
    """Infer OPC UA BuiltInType from Python value."""
    if isinstance(value, bool):
        return BUILTIN_BOOLEAN
    # Numbers default to Double (11)
    if isinstance(value, (int, float)):
        return BUILTIN_DOUBLE
    # Unknown types default to Double
    return BUILTIN_DOUBLE


def build_network_message(
    publisher_id: str,
    dataset_writer_id: int,
    sequence_number: int,
    payload_fields: dict,
    *,
    field_types: dict | None = None
) -> dict:
    """
    Build a complete OPC UA PubSub JSON NetworkMessage.
    
    Args:
        publisher_id: String identifying the publisher (robot_id or "fleet-gateway").
        dataset_writer_id: DataSetWriterId (DSW_STATE or DSW_COMMAND).
        sequence_number: Monotonically increasing sequence number per writer.
        payload_fields: Dict mapping field name -> Python value.
        field_types: Optional dict mapping field name -> BuiltInType id.
    
    Returns:
        Complete NetworkMessage dict ready for json.dumps.
    """
    # Infer types for fields
    if field_types is None:
        field_types = {}
    types = {}
    for fname, val in payload_fields.items():
        types[fname] = field_types.get(fname, _infer_type(val))
    
    # Build the single dataset message
    payload = {}
    for fname, val in payload_fields.items():
        payload[fname] = data_value(val, types[fname])
    
    dataset_message = {
        "DataSetWriterId": dataset_writer_id,
        "SequenceNumber": sequence_number,
        "MetaDataVersion": {"MajorVersion": 1, "MinorVersion": 0},
        "Timestamp": datetime.now(timezone.utc).isoformat(),
        "Status": 0,  # 0 = Good
        "MessageType": "ua-keyframe",
        "Payload": payload
    }
    
    return {
        "MessageId": str(uuid.uuid4()),
        "MessageType": "ua-data",
        "PublisherId": publisher_id,
        "Messages": [dataset_message]
    }


def encode(
    publisher_id: str,
    dataset_writer_id: int,
    sequence_number: int,
    payload_fields: dict,
    field_types: dict | None = None
) -> str:
    """Encode a NetworkMessage to JSON string."""
    return json.dumps(
        build_network_message(
            publisher_id, dataset_writer_id, sequence_number, payload_fields,
            field_types=field_types
        )
    )


def decode(raw: bytes | str) -> dict:
    """
    Decode a NetworkMessage to normalized dict.
    
    Args:
        raw: Bytes or JSON string of a NetworkMessage.
    
    Returns:
        {
            "publisher_id": str,
            "message_type": str,
            "datasets": [
                {
                    "writer_id": int,
                    "sequence_number": int,
                    "timestamp": str,
                    "status": int,
                    "fields": {field_name: python_value}
                },
                ...
            ]
        }
    
    Raises:
        ValueError: On malformed messages or non "ua-data" messages.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    
    # Validate top-level structure
    if not isinstance(msg, dict):
        raise ValueError("Message must be a JSON object")
    
    msg_type = msg.get("MessageType")
    if msg_type != "ua-data":
        raise ValueError(f"Expected MessageType 'ua-data', got '{msg_type}'")
    
    publisher_id = msg.get("PublisherId")
    if not isinstance(publisher_id, str):
        raise ValueError("PublisherId must be a string")
    
    messages = msg.get("Messages")
    if not isinstance(messages, list):
        raise ValueError("Messages must be an array")
    
    datasets = []
    for ds in messages:
        if not isinstance(ds, dict):
            raise ValueError("Each dataset must be a JSON object")
        
        writer_id = ds.get("DataSetWriterId")
        if not isinstance(writer_id, int):
            raise ValueError("DataSetWriterId must be an integer")
        
        seq = ds.get("SequenceNumber")
        if not isinstance(seq, int):
            raise ValueError("SequenceNumber must be an integer")
        
        status = ds.get("Status", 0)
        if not isinstance(status, int):
            raise ValueError("Status must be an integer")
        
        timestamp = ds.get("Timestamp", "")
        if not isinstance(timestamp, str):
            raise ValueError("Timestamp must be a string")
        
        payload = ds.get("Payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")
        
        # Extract fields, unwrapping DataValue/Variant form
        fields = {}
        for fname, dv in payload.items():
            # Handle DataValue form: {"Value": {"Type": ..., "Body": ...}}
            if isinstance(dv, dict) and "Value" in dv:
                variant_val = dv["Value"]
                if isinstance(variant_val, dict) and "Body" in variant_val:
                    fields[fname] = variant_val["Body"]
                else:
                    # Tolerate malformed variant, use raw value
                    fields[fname] = dv
            else:
                # Tolerate bare value (defensive)
                fields[fname] = dv
        
        datasets.append({
            "writer_id": writer_id,
            "sequence_number": seq,
            "timestamp": timestamp,
            "status": status,
            "fields": fields
        })
    
    return {
        "publisher_id": publisher_id,
        "message_type": msg_type,
        "datasets": datasets
    }
