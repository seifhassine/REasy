class IdManager:
    """Maps stable ``reasy_id`` values to instance IDs, which shift as instances are added or removed."""
    _instance = None
    
    @staticmethod
    def instance():
        return IdManager()
    
    def __init__(self):
        self._clear()

    def _clear(self):
        self._next_id = 1  # 0 means no instance.
        self._reasy_to_instance = {}
        self._instance_to_reasy = {}
        
    def register_instance(self, instance_id):
        if instance_id in self._instance_to_reasy:
            return self._instance_to_reasy[instance_id]
            
        reasy_id = self._next_id
        self._next_id += 1
        
        self._reasy_to_instance[reasy_id] = instance_id
        self._instance_to_reasy[instance_id] = reasy_id
        
        return reasy_id
        
    def get_instance_id(self, reasy_id):
        return self._reasy_to_instance.get(reasy_id)
    
    def update_instance_id(self, old_instance_id, new_instance_id):
        if old_instance_id not in self._instance_to_reasy:
            return
            
        reasy_id = self._instance_to_reasy[old_instance_id]
        del self._instance_to_reasy[old_instance_id]
        
        self._instance_to_reasy[new_instance_id] = reasy_id
        self._reasy_to_instance[reasy_id] = new_instance_id
    
    def remove_instance(self, instance_id):
        if instance_id not in self._instance_to_reasy:
            return
            
        reasy_id = self._instance_to_reasy[instance_id]
        del self._instance_to_reasy[instance_id]
        del self._reasy_to_instance[reasy_id]
    
    def update_all_mappings(self, id_mapping, deleted_ids=None):
        if deleted_ids is None:
            deleted_ids = set()
            
        new_instance_to_reasy = {}
        
        for instance_id in deleted_ids:
            if instance_id in self._instance_to_reasy:
                reasy_id = self._instance_to_reasy[instance_id]
                if reasy_id in self._reasy_to_instance:
                    del self._reasy_to_instance[reasy_id]
        
        for old_id, reasy_id in self._instance_to_reasy.items():
            if old_id in deleted_ids:
                continue
                
            if old_id in id_mapping:
                new_id = id_mapping[old_id]
                new_instance_to_reasy[new_id] = reasy_id
                self._reasy_to_instance[reasy_id] = new_id
            else:
                new_instance_to_reasy[old_id] = reasy_id
        
        self._instance_to_reasy = new_instance_to_reasy

    def get_reasy_id_for_instance(self, instance_id):
        if instance_id <= 0:
            return 0
        
        if instance_id in self._instance_to_reasy:
            return self._instance_to_reasy[instance_id]
                
        return self.register_instance(instance_id)


class EmbeddedIdManager(IdManager):
    """One per embedded RSZ structure."""
    
    def __init__(self, domain_id):
        """``domain_id`` is usually the parent UserData ID."""
        super().__init__()
        self._domain_id = domain_id
    
    def reset(self):
        self._clear()
