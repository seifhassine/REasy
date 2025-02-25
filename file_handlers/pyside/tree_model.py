

class DataTreeBuilder:
    """Generic tree data builder with format utilities"""
    
    @staticmethod
    def format_value(data_obj):
        """Format various data types for display"""
        if hasattr(data_obj, 'x') and hasattr(data_obj, 'y') and hasattr(data_obj, 'z'):
            if hasattr(data_obj, 'w'):
                return f"({data_obj.x:.2f}, {data_obj.y:.2f}, {data_obj.z:.2f}, {data_obj.w:.2f})"
            return f"({data_obj.x:.2f}, {data_obj.y:.2f}, {data_obj.z:.2f})"
        elif hasattr(data_obj, 'values'):
            return f"OBB: {len(data_obj.values)} values"
        elif hasattr(data_obj, 'value'):
            if isinstance(data_obj.value, str):
                return data_obj.value.rstrip('\x00')
            return str(data_obj.value)
        elif hasattr(data_obj, 'guid_str'):
            return data_obj.guid_str
        return str(data_obj)

    @staticmethod
    def create_data_node(title, value="", node_type=None, obj=None, children=None):
        """Create a standard data node"""
        node = {
            "data": [title, str(value)],
            "children": children or []
        }
        if node_type:
            node["type"] = node_type
        if obj:
            node["obj"] = obj
        return node


class ScnTreeBuilder(DataTreeBuilder):
    """SCN-specific tree building helpers"""
    
    NODES = {
        'ADVANCED': "Advanced Information",
        'GAMEOBJECTS': "GameObjects",
        'FOLDERS': "Folders",
        'SETTINGS': "Settings",
        'COMPONENTS': "Components",
        'CHILDREN': "Children",
        'DATA_BLOCK': "Data Block",
        'INSTANCES': "Instances"
    }

    @classmethod
    def create_advanced_node(cls):
        return cls.create_data_node(cls.NODES['ADVANCED'], "")

    @classmethod
    def create_gameobjects_node(cls, count):
        return cls.create_data_node(cls.NODES['GAMEOBJECTS'], f"{count} items")

    @classmethod
    def create_settings_node(cls):
        return cls.create_data_node(cls.NODES['SETTINGS'], "")

    @classmethod
    def create_components_node(cls):
        return cls.create_data_node(cls.NODES['COMPONENTS'], "")

    @classmethod
    def create_children_node(cls):
        return cls.create_data_node(cls.NODES['CHILDREN'], "")

    @classmethod 
    def create_info_section(cls, title, items, str_getter):
        """Create info section node with items"""
        children = []
        for i, item in enumerate(items):
            str_val = str_getter(item) if item.string_offset != 0 else ""
            children.append({
                "data": [f"{title.split()[0]}[{i}]", str_val]
            })
        
        return cls.create_data_node(title, f"{len(items)} items", children=children)
