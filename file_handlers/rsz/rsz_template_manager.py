import os
import json
from datetime import datetime
from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard

class RszTemplateManager:
    """
    Template manager for RSZ GameObjects
    Allows saving, categorizing, and loading GameObject templates
    """
    
    @staticmethod
    def get_template_root_directory():
        """Get the root templates directory"""
        app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(app_dir, "templates")
    
    @staticmethod
    def get_template_directory(registry_name):
        if not registry_name:
            registry_name = "default"
        
        registry_name = os.path.basename(registry_name)
        registry_name = os.path.splitext(registry_name)[0]
        registry_name = ''.join(c if c.isalnum() else '_' for c in registry_name)
        
        root_dir = RszTemplateManager.get_template_root_directory()
        return os.path.join(root_dir, registry_name)
    
    @staticmethod
    def get_metadata_path():
        root_dir = RszTemplateManager.get_template_root_directory()
        return os.path.join(root_dir, "template_metadata.json")
    
    @staticmethod
    def load_metadata():
        metadata_path = RszTemplateManager.get_metadata_path()
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading template metadata: {e}")
        
        return {"templates": {}}
    
    @staticmethod
    def save_metadata(metadata):
        metadata_path = RszTemplateManager.get_metadata_path()
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
        
        try:
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving template metadata: {e}")
            return False
    
    @staticmethod
    def export_gameobject_to_template(viewer, gameobject_id, template_name, tags=None, description=""):
        """
        Export a GameObject to a template file
        
        Args:
            viewer: RSZ viewer instance
            gameobject_id: ID of the GameObject to export
            template_name: Name for the template
            tags: List of tags for the template
            description: Optional description
            
        Returns:
            dict: Result with success status and message
        """
        if not template_name:
            return {"success": False, "message": "Template name is required"}
            
        safe_name = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in template_name)
        safe_name = safe_name.strip()
        
        if not safe_name:
            return {"success": False, "message": "Invalid template name"}
            
        registry_name = "default"
        if hasattr(viewer, "handler") and hasattr(viewer.handler, "app"):
            registry_name = os.path.basename(viewer.handler.app.settings.get("rcol_json_path", "default"))
        
        template_dir = RszTemplateManager.get_template_directory(registry_name)
        os.makedirs(template_dir, exist_ok=True)
        
        template_filename = f"{safe_name}.json"
        template_path = os.path.join(template_dir, template_filename)
        
        if os.path.exists(template_path):
            return {"success": False, "message": f"Template '{template_name}' already exists"}
        
        clipboard_data = None
        try:
            success = RszGameObjectClipboard.copy_gameobject_to_clipboard(viewer, gameobject_id)
            if not success:
                return {"success": False, "message": "Failed to copy GameObject data"}
                
            clipboard_data = RszGameObjectClipboard.get_clipboard_data(viewer)
            if not clipboard_data:
                return {"success": False, "message": "Failed to get GameObject data"}
                
            with open(template_path, 'w') as f:
                json.dump(clipboard_data, f, indent=2)
                
            metadata = RszTemplateManager.load_metadata()
            
            if "templates" not in metadata:
                metadata["templates"] = {}
                
            template_id = f"{registry_name}/{safe_name}"
            metadata["templates"][template_id] = {
                "name": template_name,
                "registry": registry_name,
                "tags": tags or [],
                "description": description,
                "created": datetime.now().isoformat(),
                "modified": datetime.now().isoformat(),
                "path": template_path
            }
            
            RszTemplateManager.save_metadata(metadata)
            
            return {
                "success": True, 
                "message": f"Template '{template_name}' exported successfully",
                "template_id": template_id
            }
            
        except Exception as e:
            return {"success": False, "message": f"Error exporting template: {str(e)}"}
    
    @staticmethod
    def import_template(viewer, template_id, parent_id=-1, new_name=None):
        """
        Import a GameObject template
        
        Args:
            viewer: RSZ viewer instance
            template_id: ID of the template to import
            parent_id: ID of the parent GameObject (-1 for root)
            new_name: Optional new name for the imported GameObject
            
        Returns:
            dict: Result with success status and message
        """
        try:
            metadata = RszTemplateManager.load_metadata()
            
            if "templates" not in metadata or template_id not in metadata["templates"]:
                return {"success": False, "message": f"Template '{template_id}' not found"}
                
            template_info = metadata["templates"][template_id]
            template_path = template_info["path"]
            
            if not os.path.exists(template_path):
                return {"success": False, "message": f"Template file not found: {template_path}"}
                
            template_registry = template_info.get("registry", "default")
            current_registry = "default"
            if hasattr(viewer, "handler") and hasattr(viewer.handler, "app"):
                current_registry = os.path.basename(viewer.handler.app.settings.get("rcol_json_path", "default"))
            
            if template_registry != current_registry:
                return {"success": False, "message": f"Cannot import template from different registry ('{template_registry}'). Current registry: '{current_registry}'"}
                
            with open(template_path, 'r') as f:
                template_data = json.load(f)
            
            if new_name and "name" in template_data:
                template_data["name"] = new_name
            elif not new_name:
                new_name = template_info["name"]
                if "name" in template_data:
                    template_data["name"] = new_name
            
            result = RszGameObjectClipboard.paste_gameobject_from_clipboard(
                viewer, parent_id, None, template_data
            )
            
            if result and result.get("success", False):
                template_info["last_used"] = datetime.now().isoformat()
                RszTemplateManager.save_metadata(metadata)
                
                return {
                    "success": True,
                    "message": f"Template '{template_info['name']}' imported successfully",
                    "gameobject_data": result
                }
            else:
                return {"success": False, "message": "Failed to create GameObject from template"}
                
        except Exception as e:
            return {"success": False, "message": f"Error importing template: {str(e)}"}
    
    @staticmethod
    def get_template_list(registry_filter=None, tag_filter=None):
        """
        Get a list of templates, optionally filtered by registry or tags
        
        Args:
            registry_filter: Optional registry name to filter by
            tag_filter: Optional tag to filter by
            
        Returns:
            list: List of template info dictionaries
        """
        metadata = RszTemplateManager.load_metadata()
        
        if "templates" not in metadata:
            return []
            
        templates = []
        for template_id, template_info in metadata["templates"].items():
            if registry_filter and template_info.get("registry") != registry_filter:
                continue
                
            if tag_filter and tag_filter not in template_info.get("tags", []):
                continue
                
            if not os.path.exists(template_info.get("path", "")):
                continue
                
            template_copy = template_info.copy()
            template_copy["id"] = template_id
            templates.append(template_copy)
            
        return sorted(templates, key=lambda t: t.get("name", ""))
    
    @staticmethod
    def get_all_tags():
        """Get a list of all tags used across templates"""
        metadata = RszTemplateManager.load_metadata()
        
        if "templates" not in metadata:
            return []
            
        all_tags = set()
        for template_info in metadata["templates"].values():
            all_tags.update(template_info.get("tags", []))
            
        return sorted(list(all_tags))
    
    @staticmethod
    def get_all_registries():
        """Get a list of all registry names used across templates"""
        metadata = RszTemplateManager.load_metadata()
        
        if "templates" not in metadata:
            return []
            
        registries = set()
        for template_info in metadata["templates"].values():
            registries.add(template_info.get("registry", "default"))
            
        return sorted(list(registries))
    
    @staticmethod
    def update_template_metadata(template_id, name=None, tags=None, description=None):
        """
        Update a template's metadata
        
        Args:
            template_id: ID of the template to update
            name: New name for the template (optional)
            tags: New tags for the template (optional)
            description: New description for the template (optional)
            
        Returns:
            bool: True if successful, False otherwise
        """
        metadata = RszTemplateManager.load_metadata()
        
        if "templates" not in metadata or template_id not in metadata["templates"]:
            return False
            
        template_info = metadata["templates"][template_id]
        
        if name is not None:
            if name != template_info["name"]:
                old_path = template_info["path"]
                
                safe_name = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in name)
                safe_name = safe_name.strip()
                
                if not safe_name:
                    return False
                    
                template_dir = os.path.dirname(old_path)
                new_filename = f"{safe_name}.json"
                new_path = os.path.join(template_dir, new_filename)
                
                try:
                    os.rename(old_path, new_path)
                    template_info["path"] = new_path
                    
                    registry = template_info["registry"]
                    new_template_id = f"{registry}/{safe_name}"
                    
                    metadata["templates"][new_template_id] = template_info
                    del metadata["templates"][template_id]
                    
                    template_info["name"] = name
                    template_id = new_template_id
                    
                except Exception as e:
                    print(f"Error renaming template file: {e}")
                    return False
            else:
                template_info["name"] = name
        
        if tags is not None:
            template_info["tags"] = tags
            
        if description is not None:
            template_info["description"] = description
            
        template_info["modified"] = datetime.now().isoformat()
        
        return RszTemplateManager.save_metadata(metadata)
    
    @staticmethod
    def delete_template(template_id):
        """
        Delete a template
        
        Args:
            template_id: ID of the template to delete
            
        Returns:
            bool: True if successful, False otherwise
        """
        metadata = RszTemplateManager.load_metadata()
        
        if "templates" not in metadata or template_id not in metadata["templates"]:
            return False
            
        template_info = metadata["templates"][template_id]
        template_path = template_info["path"]
        
        try:
            if os.path.exists(template_path):
                os.remove(template_path)
                
            del metadata["templates"][template_id]
            RszTemplateManager.save_metadata(metadata)
            
            return True
            
        except Exception as e:
            print(f"Error deleting template: {e}")
            return False
