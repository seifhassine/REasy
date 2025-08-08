"""
GUID utility functions for RSZ files.

This module provides centralized GUID handling utilities.
"""

import uuid
from utils.hex_util import guid_le_to_str, is_null_guid


def create_new_guid():
    """Create a new random GUID in little-endian bytes format."""
    return uuid.uuid4().bytes_le


def handle_guid_mapping(original_guid, guid_mapping, randomize=True):
    """
    Handle GUID mapping for copy/paste operations.
    
    Args:
        original_guid: Original GUID bytes
        guid_mapping: Dictionary mapping old GUIDs to new GUIDs
        randomize: Whether to create new random GUIDs
        
    Returns:
        bytes: New GUID bytes
    """
    if original_guid in guid_mapping:
        return guid_mapping[original_guid]
    
    if randomize:
        new_guid = create_new_guid()
    else:
        new_guid = original_guid
    
    guid_mapping[original_guid] = new_guid
    return new_guid


def process_gameobject_ref_data(guid_hex, guid_str, orig_type, guid_mapping=None, randomize_guids=True):
    """
    Process GameObjectRefData serialization with GUID handling.
    
    Args:
        guid_hex: Hex string of GUID bytes
        guid_str: String representation of GUID
        orig_type: Original type string
        guid_mapping: Optional GUID mapping dictionary
        randomize_guids: Whether to randomize GUIDs
        
    Returns:
        GameObjectRefData or None if error
    """
    from file_handlers.rsz.rsz_data_types import GameObjectRefData
    
    if not guid_hex:
        return GameObjectRefData(guid_str, None, orig_type)
    
    try:
        guid_bytes = bytes.fromhex(guid_hex)
        
        if is_null_guid(guid_bytes, guid_str):
            return GameObjectRefData(guid_str, guid_bytes, orig_type)
        
        if guid_mapping is not None:
            new_guid_bytes = handle_guid_mapping(guid_bytes, guid_mapping, randomize_guids)
            new_guid_str = guid_le_to_str(new_guid_bytes)
            return GameObjectRefData(new_guid_str, new_guid_bytes, orig_type)
        else:
            return GameObjectRefData(guid_str, guid_bytes, orig_type)
            
    except Exception as e:
        print(f"Error processing GameObjectRefData: {str(e)}")
        return GameObjectRefData(guid_str, None, orig_type)


def create_guid_data(guid_bytes):
    """
    Create a GuidData object from GUID bytes.
    
    Args:
        guid_bytes: GUID in little-endian bytes format
        
    Returns:
        GuidData object
    """
    from file_handlers.rsz.rsz_data_types import GuidData
    
    guid_str = guid_le_to_str(guid_bytes)
    return GuidData(guid_str, guid_bytes)