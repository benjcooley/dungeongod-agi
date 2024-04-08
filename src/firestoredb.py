from urllib.parse import quote, unquote
import traceback

from db_access import Db

from google.cloud import firestore
from typing import Any, cast, Dict, Optional

class FirestoreDb(Db):
    def __init__(self):
        self.db = firestore.Client(project="dungeongod1")

    def get_ref(self, path: str, force_doc: bool = False):
        # Split the path and iterate to create document and collection references
        segments = path.split('/')
        # Note: 
        if force_doc and len(segments) % 2 == 1:
            segments = segments[:-1]
        ref = self.db
        for i, segment in enumerate(segments):
            if i % 2 == 0:
                # Document reference
                ref = cast(firestore.DocumentReference, ref).collection(quote(segment))
            else:
                # Collection reference
                ref = cast(firestore.CollectionReference, ref).document(quote(segment))       
        return ref

    async def exists(self, path: str) -> bool:
        try:
            ref = cast(firestore.DocumentReference, self.get_ref(path, force_doc=True))
            doc = ref.get()
            return doc.exists
        except Exception as e:
            print(f"Error: firestore exists({path})")
            traceback.print_exception(e)
            return False

    async def get(self, path: str) -> Dict[str, Any]|None:
        try:
            ref = cast(firestore.DocumentReference, self.get_ref(path, force_doc=True))
            doc = ref.get()
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            print(f"Error: firestore get({path})")
            traceback.print_exception(e)
            return None

    async def put(self, path: str, data: Dict[str, Any]) -> None:
        try:
            ref = cast(firestore.DocumentReference, self.get_ref(path, force_doc=True))
            ref.set(data)
        except Exception as e:
            print(f"Error: firestore put({path})")
            traceback.print_exception(e)
            return

    async def delete(self, path: str) -> bool:
        try:
            ref = cast(firestore.DocumentReference, self.get_ref(path, force_doc=True))
            ref.delete()
            return True
        except Exception as e:
            print(f"Error: firestore delete({path})")
            traceback.print_exception(e)
            return False        

    async def get_list(self, collection_path: str) -> list[str]:
        try:
            collection_ref = cast(firestore.CollectionReference, self.db.collection(collection_path))
            doc_ids = [unquote(doc.id) for doc in collection_ref.list_documents()]
            return doc_ids
        except Exception as e:
            print(str(e))
            return []
    
