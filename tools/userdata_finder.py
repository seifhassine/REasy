#Tool used in parsing research


import json

def find_userdata_fields(json_file):
    with open(json_file, 'r') as f:
        data = json.load(f)
    
    userdata_types = set()
    
    for type_id, type_info in data.items():
        if not isinstance(type_info, dict) or 'fields' not in type_info:
            continue
            
        for field in type_info['fields']:
            if (field.get('type') == 'UserData' and
                not field.get('array', True) and  
                field.get('size') == 4 and
                field.get('align') == 4):
                
                original_type = field.get('original_type')
                if original_type:
                    userdata_types.add(original_type)
    
    print("Found UserData types matching criteria (non-array, size=4, align=4):")
    for t in sorted(userdata_types):
        print(f"- {t}")
    
    return userdata_types

if __name__ == "__main__":
    json_path = "rszre4_reasy.json"
    find_userdata_fields(json_path)
