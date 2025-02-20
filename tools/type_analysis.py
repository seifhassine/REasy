
def extract_distinct_field_types(type_json: dict) -> set:
    """Extract all distinct field types from the type definitions JSON
    
    Args:
        type_json: Dictionary containing type definitions
        
    Returns:
        Set of unique field type strings
    """
    types = set()
    
    for type_id, type_info in type_json.items():
        if "fields" in type_info:
            for field in type_info["fields"]:
                if "type" in field:
                    types.add(field["type"])
                    
    return types

if __name__ == "__main__":
    import json
    with open("rszre4_reasy.json") as f:
        type_json = json.load(f)
        
    distinct_types = extract_distinct_field_types(type_json)
    print("Distinct field types:")
    for t in sorted(distinct_types):
        print(f"  {t}")
