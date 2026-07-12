"""Shared tree operations for the direct RSZ editor UI."""

from file_handlers.pyside.tree_model import DataTreeBuilder
from file_handlers.rsz.rsz_data_types import is_reference_type


def create_array_element_node(viewer, index, element, embedded_context=None):
    """Create the canonical tree node for an RSZ array element."""
    if is_reference_type(element) and hasattr(viewer, "_handle_reference_in_array"):
        domain_id = getattr(embedded_context, "instance_id", None)
        return viewer._handle_reference_in_array(
            index, element, embedded_context, domain_id
        )

    return DataTreeBuilder.create_data_node(
        f"{index}: ", "", element.__class__.__name__, element
    )


def append_array_element_node(
    viewer,
    array_item,
    element,
    *,
    tree=None,
    embedded_context=None,
    node_factory=None,
    initialize_widget=False,
    scroll_to_child=False,
    expand_child=False,
    respect_deferred=True,
    child_index_from_row=False,
    model=None,
    widget_factory=None,
):
    """Append and reveal one array element in an RSZ editor tree.

    ``node_factory`` is reserved for embedded RSZ nodes that need additional
    context metadata. All model, deferred-node, expansion, scrolling, and editor
    widget plumbing is kept here so creation and clipboard paste stay identical.
    """
    tree = tree or getattr(viewer, "tree", None)
    if model is None:
        model_getter = getattr(tree, "model", None)
        model = model_getter() if callable(model_getter) else None
    if not model or not hasattr(array_item, "raw"):
        return False

    raw = array_item.raw if isinstance(array_item.raw, dict) else {}
    array_data = raw.get("obj")
    if not array_data or not hasattr(array_data, "values"):
        return False

    if respect_deferred and (
        getattr(array_item, "_deferred_builder", None)
        and not getattr(array_item, "_children_built", False)
    ):
        array_index = model.getIndexFromItem(array_item)
        tree.expand(array_index)
        return True

    element_index = len(array_data.values) - 1
    if embedded_context is None:
        embedded_context = raw.get("embedded_context")
    if node_factory is None:
        node = create_array_element_node(
            viewer, element_index, element, embedded_context
        )
    else:
        node = node_factory(element_index, element, embedded_context)

    model.addChild(array_item, node)
    array_index = model.getIndexFromItem(array_item)
    tree.expand(array_index)

    if not (initialize_widget or scroll_to_child or expand_child):
        return True
    children = getattr(array_item, "children", ())
    if not children:
        return True
    child_index = (
        model.index(element_index, 0, array_index)
        if child_index_from_row
        else model.getIndexFromItem(children[-1])
    )
    if expand_child:
        tree.expand(child_index)
    if scroll_to_child and hasattr(tree, "scrollTo"):
        tree.scrollTo(child_index)
    if not initialize_widget or not child_index.isValid():
        return True

    if widget_factory is None:
        from file_handlers.pyside.tree_widgets import TreeWidgetFactory

        widget_factory = TreeWidgetFactory

    child_item = child_index.internalPointer()
    if not child_item or widget_factory.should_skip_widget(child_item):
        return True
    name_text = child_item.data[0] if getattr(child_item, "data", None) else ""
    child_raw = child_item.raw if isinstance(child_item.raw, dict) else {}
    widget = widget_factory.create_widget(
        child_raw.get("type", ""),
        child_raw.get("obj"),
        name_text,
        tree,
        getattr(tree, "parent_modified_callback", None),
    )
    if widget:
        tree.setIndexWidget(child_index, widget)
    return True
