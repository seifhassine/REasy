"""
ID Manager - Maintains stable IDs that persist despite backend changes
"""

class IdManager:
    """
    Manages stable IDs for REasy that won't change when backend IDs shift.
    Maps unchanging 'reasy_id' values to dynamic 'instance_id' values.
    """
    _instance = None
    
    @staticmethod
    def instance():
        return IdManager()
    
    def __init__(self):
        self._next_id = 1  # Start at 1 (0 can be reserved for invalid)
        self._reasy_to_instance = {}  # Map reasy_id -> instance_id
        self._instance_to_reasy = {}  # Map instance_id -> reasy_id
        
    def register_instance(self, instance_id):
        """
        Register an instance_id and get a stable reasy_id
        
        If the instance_id is already registered, returns its existing reasy_id
        Otherwise, creates a new mapping
        """
        if instance_id in self._instance_to_reasy:
            return self._instance_to_reasy[instance_id]
            
        reasy_id = self._next_id
        self._next_id += 1
        
        self._reasy_to_instance[reasy_id] = instance_id
        self._instance_to_reasy[instance_id] = reasy_id
        
        return reasy_id
        
    def force_register_instance(self, instance_id, reasy_id):
        if instance_id in self._instance_to_reasy:
            old_reasy = self._instance_to_reasy[instance_id]
            if old_reasy in self._reasy_to_instance:
                del self._reasy_to_instance[old_reasy]
            
        if reasy_id in self._reasy_to_instance:
            old_instance = self._reasy_to_instance[reasy_id]
            if old_instance in self._instance_to_reasy:
                del self._instance_to_reasy[old_instance]
        
        self._reasy_to_instance[reasy_id] = instance_id
        self._instance_to_reasy[instance_id] = reasy_id
        
        if reasy_id >= self._next_id:
            self._next_id = reasy_id + 1
            
        return reasy_id
    
    def get_instance_id(self, reasy_id):
        """Get current instance_id for a reasy_id"""
        return self._reasy_to_instance.get(reasy_id)
    
    def update_instance_id(self, old_instance_id, new_instance_id):
        """Update a mapping when an instance_id changes"""
        if old_instance_id not in self._instance_to_reasy:
            return
            
        reasy_id = self._instance_to_reasy[old_instance_id]
        del self._instance_to_reasy[old_instance_id]
        
        self._instance_to_reasy[new_instance_id] = reasy_id
        self._reasy_to_instance[reasy_id] = new_instance_id
    
    def remove_instance(self, instance_id):
        """Remove an instance from the mappings (when it's deleted)"""
        if instance_id not in self._instance_to_reasy:
            return
            
        reasy_id = self._instance_to_reasy[instance_id]
        del self._instance_to_reasy[instance_id]
        del self._reasy_to_instance[reasy_id]
    
    def update_all_mappings(self, id_mapping, deleted_ids=None):
        """
        Update all mappings based on a provided mapping dictionary
        
        Args:
            id_mapping: Dict mapping old_instance_id -> new_instance_id
            deleted_ids: Set of instance IDs that were deleted
        """
        if deleted_ids is None:
            deleted_ids = set()
            
        # Create new mappings
        new_instance_to_reasy = {}
        
        # First remove deleted IDs
        for instance_id in deleted_ids:
            if instance_id in self._instance_to_reasy:
                reasy_id = self._instance_to_reasy[instance_id]
                if reasy_id in self._reasy_to_instance:
                    del self._reasy_to_instance[reasy_id]
        
        # Update all instance ID references
        for old_id, reasy_id in self._instance_to_reasy.items():
            if old_id in deleted_ids:
                continue
                
            if old_id in id_mapping:
                new_id = id_mapping[old_id]
                new_instance_to_reasy[new_id] = reasy_id
                self._reasy_to_instance[reasy_id] = new_id
            else:
                # Keep unchanged mappings
                new_instance_to_reasy[old_id] = reasy_id
        
        self._instance_to_reasy = new_instance_to_reasy

    def get_reasy_id_for_instance(self, instance_id):
        """Get reasy_id for an instance_id, creating one if needed"""
        if instance_id <= 0:
            return 0
        
        if instance_id in self._instance_to_reasy:
            return self._instance_to_reasy[instance_id]
                
        return self.register_instance(instance_id)


class EmbeddedIdManager:
    """
    Manages stable IDs for embedded RSZ structures
    
    Each embedded RSZ structure has its own ID manager to prevent
    collisions between different embedded structures and the main RSZ.
    """
    
    def __init__(self, domain_id):
        """
        Initialize a new embedded ID manager
        
        Args:
            domain_id: Unique identifier for this embedded RSZ structure (typically the parent UserData ID)
        """
        self._domain_id = domain_id
        self._next_id = 1  # Start at 1 (0 is reserved)
        self._reasy_to_instance = {}  # Map reasy_id -> instance_id
        self._instance_to_reasy = {}  # Map instance_id -> reasy_id
    
    def register_instance(self, instance_id):
        """
        Register an instance_id and get a stable reasy_id
        
        If the instance_id is already registered, returns its existing reasy_id
        Otherwise, creates a new mapping
        """
        if instance_id in self._instance_to_reasy:
            return self._instance_to_reasy[instance_id]
            
        reasy_id = self._next_id
        self._next_id += 1
        
        self._reasy_to_instance[reasy_id] = instance_id
        self._instance_to_reasy[instance_id] = reasy_id
        
        return reasy_id
    
    def reset(self):
        """Reset all mappings"""
        self._next_id = 1
        self._reasy_to_instance = {}
        self._instance_to_reasy = {}
