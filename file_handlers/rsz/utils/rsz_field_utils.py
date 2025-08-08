"""
Utility functions for RSZ field operations.

This module provides common field manipulation utilities to reduce code duplication.
"""

from file_handlers.rsz.rsz_data_types import is_reference_type, is_array_type


def update_field_references(fields, reference_updater):
    """
    Update all references in fields using the provided updater function.
    
    Args:
        fields: Dictionary of field_name -> field_data
        reference_updater: Function that takes (field_data) and updates its references
    """
    for field_name, field_data in fields.items():
        if is_reference_type(field_data):
            reference_updater(field_data)
        elif is_array_type(field_data):
            for element in field_data.values:
                if is_reference_type(element):
                    reference_updater(element)


def collect_field_references(fields, reference_collector):
    """
    Collect all references from fields using the provided collector function.
    
    Args:
        fields: Dictionary of field_name -> field_data
        reference_collector: Function that takes (field_data) and collects its references
    """
    for field_name, field_data in fields.items():
        if is_reference_type(field_data):
            reference_collector(field_data)
        elif is_array_type(field_data):
            for element in field_data.values:
                if is_reference_type(element):
                    reference_collector(element)


def update_references_with_mapping(fields, id_mapping, deleted_ids=None):
    """
    Update all references in fields based on ID mapping and deleted IDs.
    
    Args:
        fields: Dictionary of field_name -> field_data
        id_mapping: Dictionary mapping old IDs to new IDs
        deleted_ids: Optional set of deleted IDs (will be set to 0)
    """
    def updater(ref_obj):
        if ref_obj.value > 0:
            if deleted_ids and ref_obj.value in deleted_ids:
                ref_obj.value = 0
            elif ref_obj.value in id_mapping:
                ref_obj.value = id_mapping[ref_obj.value]
    
    update_field_references(fields, updater)


def shift_references_above_threshold(fields, threshold, offset=1):
    """
    Shift all references above a threshold by the given offset.
    
    Args:
        fields: Dictionary of field_name -> field_data
        threshold: References >= this value will be shifted
        offset: Amount to shift by (default: 1)
    """
    def updater(ref_obj):
        if ref_obj.value >= threshold:
            ref_obj.value += offset
    
    update_field_references(fields, updater)


def process_array_elements(array_data, element_processor):
    """
    Process all elements in an ArrayData object.
    
    Args:
        array_data: ArrayData object to process
        element_processor: Function that takes (element, index) and processes it
    """
    if is_array_type(array_data):
        for i, element in enumerate(array_data.values):
            element_processor(element, i)


def count_reference_types_in_array(array_data):
    """
    Count reference type elements (ObjectData/UserDataData) in an array.
    
    Args:
        array_data: ArrayData object to analyze
        
    Returns:
        dict: {'object': count, 'userdata': count}
    """
    counts = {'object': 0, 'userdata': 0}
    
    def counter(element, index):
        if hasattr(element, '__class__'):
            class_name = element.__class__.__name__
            if class_name == 'ObjectData':
                counts['object'] += 1
            elif class_name == 'UserDataData':
                counts['userdata'] += 1
    
    process_array_elements(array_data, counter)
    return counts