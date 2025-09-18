

class DataTreeBuilder:
    """Generic tree data builder with format utilities"""

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

    @staticmethod
    def create_text_node(text, node_type=None, obj=None):
        """Create a node that only displays text in the primary column."""
        return DataTreeBuilder.create_data_node(text, "", node_type=node_type, obj=obj)

    @staticmethod
    def create_children_from_pairs(pairs, formatter="{label}: {value}"):
        """Create simple text children from (label, value) pairs."""
        return [
            DataTreeBuilder.create_text_node(
                formatter.format(label=label, value=value)
            )
            for label, value in pairs
        ]

    @staticmethod
    def create_count_node(title, count, unit="items", node_type=None, obj=None):
        """Create a node that displays a count with a shared label format."""
        suffix = "" if not unit else f" {unit}"
        value = f"{count}{suffix}"
        return DataTreeBuilder.create_data_node(title, value, node_type=node_type, obj=obj)

    @classmethod
    def create_branch_from_pairs(
        cls, title, pairs, node_type=None, obj=None, formatter="{label}: {value}"
    ):
        """Create a branch node populated with children from (label, value) pairs."""
        children = cls.create_children_from_pairs(pairs, formatter)
        return cls.create_data_node(title, "", node_type=node_type, obj=obj, children=children)