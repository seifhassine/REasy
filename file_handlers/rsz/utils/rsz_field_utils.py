"""
Utility functions for RSZ field operations.

This module provides common field manipulation utilities to reduce code duplication.
"""

from file_handlers.rsz.rsz_data_types import (
    ObjectData,
    UserDataData,
    is_reference_type,
    is_array_type,
)


def iter_field_reference_entries(fields):
    """
    Yield references from direct fields and one level of ArrayData elements.

    Yields:
        tuple: (field_name, reference_object, array_index)
        array_index is None for direct fields.
    """
    for field_name, field_data in fields.items():
        if is_reference_type(field_data):
            yield field_name, field_data, None
        elif is_array_type(field_data):
            for index, element in enumerate(field_data.values):
                if is_reference_type(element):
                    yield field_name, element, index


def iter_field_references(fields):
    """Yield all direct and array-element reference objects in fields."""
    for _, ref_obj, _ in iter_field_reference_entries(fields):
        yield ref_obj


def update_field_references(fields, reference_updater):
    """
    Update all references in fields using the provided updater function.
    
    Args:
        fields: Dictionary of field_name -> field_data
        reference_updater: Function that takes (field_data) and updates its references
    """
    for ref_obj in iter_field_references(fields):
        reference_updater(ref_obj)


def collect_field_references(fields, reference_collector):
    """
    Collect all references from fields using the provided collector function.
    
    Args:
        fields: Dictionary of field_name -> field_data
        reference_collector: Function that takes (field_data) and collects its references
    """
    for ref_obj in iter_field_references(fields):
        reference_collector(ref_obj)


def collect_reference_values(fields, reference_type=None, positive_only=True):
    """
    Collect reference values from fields.

    Args:
        fields: Dictionary of field_name -> field_data
        reference_type: Optional class to filter by (ObjectData/UserDataData)
        positive_only: When True, only collect values greater than 0

    Returns:
        set: Collected reference values
    """
    values = set()
    for ref_obj in iter_field_references(fields):
        if reference_type is not None and not isinstance(ref_obj, reference_type):
            continue
        if positive_only and ref_obj.value <= 0:
            continue
        values.add(ref_obj.value)
    return values


def collect_object_reference_values(fields, positive_only=True):
    """Collect ObjectData reference values from fields."""
    return collect_reference_values(fields, ObjectData, positive_only)


def collect_userdata_reference_values(fields, positive_only=True):
    """Collect UserDataData reference values from fields."""
    return collect_reference_values(fields, UserDataData, positive_only)


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


def update_references_of_type(fields, id_mapping, reference_type):
    """
    Update references of a specific reference class using an ID mapping.

    Args:
        fields: Dictionary of field_name -> field_data
        id_mapping: Dictionary mapping old IDs to new IDs
        reference_type: Reference class to update
    """
    for ref_obj in iter_field_references(fields):
        if isinstance(ref_obj, reference_type) and ref_obj.value in id_mapping:
            ref_obj.value = id_mapping[ref_obj.value]


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