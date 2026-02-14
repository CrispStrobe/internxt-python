#!/usr/bin/env python3
"""
internxt_cli/utils/api.py
API client matching Internxt SDK
"""

import requests
import json
import sys
import os
from typing import Dict, Any, Optional, List

from config.config import config_service

# utils/api.py

class ApiClient:
    def __init__(self):
        self.session = requests.Session()
        self.drive_api_url = config_service.get('DRIVE_NEW_API_URL')
        self.network_url = config_service.get('NETWORK_URL')
        
        # Match rclone adapter - use internxt-client header only
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'internxt-client': 'internxt-cli',  # This is the key header
        })

    def _make_request(self, method: str, url: str, data: Optional[Any] = None, 
                    headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None, 
                    auth: Optional[tuple] = None, is_json=True) -> requests.Response:
        """Central request handler, returns the full response object."""
        try:
            request_headers = self.session.headers.copy()
            if headers:
                request_headers.update(headers)
            
            # If basic auth is provided, it overrides the session's Bearer token
            if auth:
                request_headers.pop('Authorization', None)

            json_payload = data if is_json else None
            data_payload = data if not is_json else None

            # Simply use session.request() - much cleaner!
            response = self.session.request(
                method, 
                url, 
                json=json_payload, 
                data=data_payload,
                params=params,  # ← params goes here
                headers=request_headers, 
                auth=auth, 
                timeout=300
            )
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            error_message = f"HTTP {e.response.status_code} Error"
            try: 
                error_message = e.response.json().get("message", "Unknown Error")
            except json.JSONDecodeError: 
                pass
            raise ValueError(f"API Error: {error_message}") from e
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Network request failed for {url}: {e}") from e

    # Robust wrapper to ensure return is always a dictionary
    def post(self, url: str, data=None, **kwargs) -> Dict[str, Any]:
        resp = self._make_request("POST", url, data=data, **kwargs)
        return resp.json() if resp.content else {}

    def get(self, url: str, **kwargs) -> Dict[str, Any]:
        resp = self._make_request("GET", url, **kwargs)
        return resp.json() if resp.content else {}

    def robust_request(self, method, url, **kwargs):
        """
        Wrapper for requests with automatic 401 refresh logic and high verbosity.
        """
        try:
            if 'headers' not in kwargs:
                kwargs['headers'] = self.session.headers.copy()

            print(f"📡 DEBUG: {method} {url}")
            response = self.session.request(method, url, **kwargs)
            
            # Handle Expired Token
            if response.status_code == 401:
                print("🕒 DEBUG: Received 401. Attempting token rotation...")
                from services.auth import auth_service
                auth_service.refresh_tokens()
                
                # Re-fetch new credentials and retry
                creds = config_service.read_user_credentials()
                kwargs['headers']['Authorization'] = f"Bearer {creds['token']}"
                print(f"🔄 DEBUG: Retrying {method} {url} with new token.")
                response = self.session.request(method, url, **kwargs)
                
            response.raise_for_status()
            return response.json() if response.content else {}
            
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            body = e.response.text
            print(f"❌ DEBUG: HTTP {status} Failure. Body: {body}")
            if status == 402:
                print("💰 DEBUG: Payment Required. Check subscription status.")
            elif status == 409:
                print("🚫 DEBUG: Conflict error. Item already exists.")
            raise ValueError(f"API Error ({status}): {body}")
        except Exception as e:
            print(f"💥 DEBUG: Request Exception: {str(e)}")
            raise

    def set_auth_tokens(self, token: Optional[str], new_token: Optional[str]):
        """Sets the auth token for subsequent requests."""
        if new_token:
            self.session.headers['Authorization'] = f'Bearer {new_token}'
        else:
            self.session.headers.pop('Authorization', None)

    def refresh_token(self, current_new_token: str) -> Dict[str, Any]:
        """
        Refreshes the auth token.
        The Internxt "newToken" acts as a long-lived refresh token.
        We send it as the Bearer token to the refresh endpoint.
        """
        url = f"{self.drive_api_url}/users/refresh" 
        headers = {'Authorization': f'Bearer {current_new_token}'}
        
        # Make the GET request *without* the default session auth
        response = self._make_request("GET", url, headers=headers)
        
        return response.json() if response.content else {}


    def security_details(self, email: str) -> Dict[str, Any]:
        """Strictly formatted security check."""
        url = f"{self.drive_api_url}/auth/login"
        # The email must be lowercase and stripped
        payload = {'email': email.lower().strip()}
        return self.post(url, data=payload)

    # ========== HTTP VERB METHODS ==========

    def delete(self, url: str, headers: Dict[str, str] = None, auth: Optional[tuple] = None) -> Dict[str, Any]:
        """Make DELETE request and return JSON"""
        response = self._make_request("DELETE", url, headers=headers, auth=auth)
        return response.json() if response.content else {}
    
    def put(self, url: str, data: Dict[str, Any] = None, headers: Dict[str, str] = None, auth: Optional[tuple] = None) -> Dict[str, Any]:
        """Make PUT request and return JSON"""
        response = self._make_request("PUT", url, data=data, headers=headers, auth=auth)
        return response.json() if response.content else {}

    def patch(self, url: str, data: Dict[str, Any] = None, headers: Dict[str, str] = None, auth: Optional[tuple] = None) -> Dict[str, Any]:
        """Make PATCH request and return JSON"""
        response = self._make_request("PATCH", url, data=data, headers=headers, auth=auth)
        return response.json() if response.content else {}

    # ========== AUTH API ENDPOINTS ==========

    def login_access(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Performs the final login."""
        url = f"{self.drive_api_url}/auth/login/access"
        # Ensure the email inside the nested payload is also clean
        if 'email' in payload:
            payload['email'] = payload['email'].lower().strip()
        return self.post(url, data=payload)

    # ========== STORAGE API ENDPOINTS ==========
    
    def get_folder_folders(self, folder_uuid: str, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        """Get subfolders in folder"""
        url = f"{self.drive_api_url}/folders/content/{folder_uuid}/folders"
        params = {'offset': offset, 'limit': limit, 'sort': 'plainName', 'direction': 'ASC'}
        return self.get(url, params=params)

    def get_folder_files(self, folder_uuid: str, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        """Get files in folder"""
        url = f"{self.drive_api_url}/folders/content/{folder_uuid}/files"
        params = {'offset': offset, 'limit': limit, 'sort': 'plainName', 'direction': 'ASC'}
        return self.get(url, params=params)

    def create_folder(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create new folder using a full payload (for timestamp support)
        Real SDK: POST /folders
        """
        url = f"{self.drive_api_url}/folders"
        # Ensure 'plainName' and 'parentFolderUuid' are in payload,
        # but also allow 'creationTime', 'modificationTime', etc.
        if 'plainName' not in payload or 'parentFolderUuid' not in payload:
             raise ValueError("create_folder payload must include 'plainName' and 'parentFolderUuid'")
        
        return self.post(url, data=payload)

    # ========== METADATA OPERATIONS ==========
    
    def get_file_metadata(self, file_uuid: str) -> Dict[str, Any]:
        """Get file metadata"""
        url = f"{self.drive_api_url}/files/{file_uuid}/meta"
        return self.get(url)

    def get_folder_metadata(self, folder_uuid: str) -> Dict[str, Any]:
        """Get folder metadata"""
        url = f"{self.drive_api_url}/folders/{folder_uuid}/meta"
        return self.get(url)

    # ========== UPDATE METADATA OPERATIONS ==========

    def update_file_metadata(self, file_uuid: str, update_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update file metadata 
        Real SDK: PUT /files/{fileUuid}/meta
        """
        url = f"{self.drive_api_url}/files/{file_uuid}/meta"
        return self.put(url, data=update_data)

    def update_folder_metadata(self, folder_uuid: str, update_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update folder metadata
        Real SDK: PUT /folders/{folderUuid}/meta  
        """
        url = f"{self.drive_api_url}/folders/{folder_uuid}/meta"
        return self.put(url, data=update_data)
    
    def get_folder_ancestors(self, folder_uuid: str) -> List[Dict[str, Any]]:
        """
        Get all parent folders (ancestors) of a given folder
        Ref: GET /folders/{uuid}/ancestors
        """
        url = f"{self.drive_api_url}/folders/{folder_uuid}/ancestors"
        response = self.get(url)
        # The API returns a list directly
        return response if isinstance(response, list) else []

    # ========== MOVE OPERATIONS ==========
    
    def move_file(self, file_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """
        Move file to different folder
        Real SDK: PATCH /files/{fileUuid} with destinationFolder field
        """
        url = f"{self.drive_api_url}/files/{file_uuid}"
        data = {'destinationFolder': destination_folder_uuid}  # field name
        return self.patch(url, data)

    def move_folder(self, folder_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """
        Move folder to different folder  
        Real SDK: PATCH /folders/{folderUuid} with destinationFolder field
        """
        url = f"{self.drive_api_url}/folders/{folder_uuid}"
        data = {'destinationFolder': destination_folder_uuid}  # field name
        return self.patch(url, data)

    def rename_file(self, file_uuid: str, new_name: str, new_type: str = None) -> Dict[str, Any]:
        """
        Rename file using metadata update
        Real SDK: PUT /files/{fileUuid}/meta
        """
        data = {'plainName': new_name}
        if new_type is not None:
            data['type'] = new_type
        return self.update_file_metadata(file_uuid, data)

    def rename_folder(self, folder_uuid: str, new_name: str) -> Dict[str, Any]:
        """
        Rename folder using metadata update
        Real SDK: PUT /folders/{folderUuid}/meta
        """
        data = {'plainName': new_name}
        return self.update_folder_metadata(folder_uuid, data)

    # ========== DELETE/TRASH OPERATIONS ==========
    
    def delete_file(self, file_uuid: str) -> Dict[str, Any]:
        """
        Delete file (moves to trash)
        Real SDK: DELETE /files/{fileId}
        """
        url = f"{self.drive_api_url}/files/{file_uuid}"
        return self.delete(url)

    def delete_folder(self, folder_uuid: str) -> Dict[str, Any]:
        """
        Delete folder (moves to trash)
        Real SDK: DELETE /folders/{folderId}
        """
        url = f"{self.drive_api_url}/folders/{folder_uuid}"
        return self.delete(url)

    # ========== TRASH OPERATIONS ==========
    
    def trash_file(self, file_uuid: str) -> Dict[str, Any]:
        """
        Move individual file to trash
        Real SDK: Only has bulk trash, so we use that with single item
        """
        return self.trash_items({'items': [{'uuid': file_uuid, 'type': 'file'}]})

    def trash_folder(self, folder_uuid: str) -> Dict[str, Any]:
        """
        Move individual folder to trash  
        Real SDK: Only has bulk trash, so we use that with single item
        """
        return self.trash_items({'items': [{'uuid': folder_uuid, 'type': 'folder'}]})

    def trash_items(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add items to trash - bulk operation
        Real SDK: POST /storage/trash/add
        """
        url = f"{self.drive_api_url}/storage/trash/add"
        return self.post(url, data=payload)

    # ========== TRASH MANAGEMENT ==========
    
    def get_trash_content(self, offset: int = 0, limit: int = 50, item_type: str = 'both') -> Dict[str, Any]:
        """
        Get trash content
        Real SDK: GET /storage/trash with pagination params
        """
        url = f"{self.drive_api_url}/storage/trash/paginated"
        params = {'offset': offset, 'limit': limit, 'type': item_type}
        return self.get(url, params=params)

    def clear_trash(self) -> Dict[str, Any]:
        """
        Clear all items from trash permanently
        Real SDK: DELETE /storage/trash/all
        """
        url = f"{self.drive_api_url}/storage/trash/all"
        return self.delete(url)

    def restore_item(self, item_uuid: str, item_type: str, destination_folder_uuid: str = None) -> Dict[str, Any]:
        """
        Restore item from trash - keeping original implementation
        (Not clearly shown in provided SDK snippets)
        """
        url = f"{self.drive_api_url}/trash/restore"
        data = {
            'uuid': item_uuid,
            'type': item_type,
            'destinationFolderUuid': destination_folder_uuid
        }
        return self.post(url, data)

    def delete_permanently(self, item_uuid: str, item_type: str) -> Dict[str, Any]:
        """
        Permanently delete item from trash
        Real SDK: DELETE /storage/trash with items array
        """
        url = f"{self.drive_api_url}/storage/trash"
        data = {'items': [{'uuid': item_uuid, 'type': item_type}]}
        return self.delete(url, headers={'Content-Type': 'application/json'})

    # ========== PATH-BASED OPERATIONS ==========

    def get_folder_by_path(self, folder_path: str) -> Dict[str, Any]:
        """
        Get folder by path
        Real SDK: GET /folders/meta?path={folderPath}
        """
        url = f"{self.drive_api_url}/folders/meta"
        params = {'path': folder_path}
        return self.get(url, params=params)

    def get_file_by_path(self, file_path: str) -> Dict[str, Any]:
        """
        Get file by path  
        Real SDK: GET /files/meta?path={filePath}
        """
        url = f"{self.drive_api_url}/files/meta"
        params = {'path': file_path}
        return self.get(url, params=params)

    # ========== NETWORK API ENDPOINTS ==========
    
    def start_upload(self, bucket_id: str, file_size: int, auth: tuple) -> Dict[str, Any]:
        """
        Gets upload URLs for a file
        Real SDK: POST /v2/buckets/{bucketId}/files/start?multiparts=1
        """
        url = f"{self.network_url}/v2/buckets/{bucket_id}/files/start?multiparts=1"
        data = {'uploads': [{'index': 0, 'size': file_size}]}
        return self.post(url, data, auth=auth)

    def upload_chunk(self, upload_url: str, chunk_data: bytes):
        """Uploads a raw chunk of data using PUT. No auth needed for pre-signed URL."""
        response = requests.put(upload_url, data=chunk_data, headers={'Content-Type': 'application/octet-stream'}, timeout=300)
        response.raise_for_status()

    def finish_upload(self, bucket_id: str, payload: Dict[str, Any], auth: tuple) -> Dict[str, Any]:
        """
        Finalizes an upload
        Real SDK: POST /v2/buckets/{bucketId}/files/finish
        """
        url = f"{self.network_url}/v2/buckets/{bucket_id}/files/finish"
        return self.post(url, data=payload, auth=auth)

    def get_download_links(self, bucket_id: str, file_id: str, auth: tuple) -> Dict[str, Any]:
        """
        Gets download URLs for a file
        Real SDK: GET /buckets/{bucketId}/files/{fileId}/info with x-api-version: 2 header
        """
        url = f"{self.network_url}/buckets/{bucket_id}/files/{file_id}/info"
        headers = {'x-api-version': '2'}
        return self.get(url, headers=headers, auth=auth)

    def download_chunk(self, download_url: str) -> bytes:
        """Downloads a raw chunk of data. No auth needed for pre-signed URL."""
        response = requests.get(download_url, timeout=300)
        response.raise_for_status()
        return response.content

    # ========== FILE OPERATIONS ==========
    
    def create_file_entry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Creates file metadata entry  
        Real SDK: POST /files
        """
        url = f"{self.drive_api_url}/files"
        return self.post(url, data=payload)

    def replace_file(self, file_uuid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Replace file with new content
        Real SDK: PUT /files/{uuid}
        """
        url = f"{self.drive_api_url}/files/{file_uuid}"
        return self.put(url, data=payload)

    # ========== SEARCH OPERATIONS ==========

    def search_files(self, query: str, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        """
        Search for files and folders
        Real SDK: GET /fuzzy/{search}?offset={offset}
        """
        url = f"{self.drive_api_url}/fuzzy/{query}"
        params = {'offset': offset}
        return self.get(url, params=params)

    # ========== UTILITY OPERATIONS ==========
    
    def get_storage_usage(self) -> Dict[str, Any]:
        """Get storage usage information"""
        url = f"{self.drive_api_url}/users/usage"
        return self.get(url)

    def get_user_info(self) -> Dict[str, Any]:
        """Get current user information"""
        url = f"{self.drive_api_url}/users/me"
        return self.get(url)

    def health_check(self) -> Dict[str, Any]:
        """Basic health check"""
        try:
            return self.get_user_info()
        except:
            return {'status': 'error', 'message': 'Health check failed'}

    # ========== WEBDAV COMPATIBILITY LAYER ==========
    
    def move_item(self, item_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """WebDAV compatibility: Move file or folder"""
        # Try as file first
        try:
            return self.move_file(item_uuid, destination_folder_uuid)
        except:
            # If file move fails, try as folder
            return self.move_folder(item_uuid, destination_folder_uuid)

    def rename_item(self, item_uuid: str, new_name: str) -> Dict[str, Any]:
        """WebDAV compatibility: Rename file or folder"""
        # Try as file first (with name/type parsing)
        try:
            if '.' in new_name:
                name_parts = new_name.rsplit('.', 1)
                plain_name = name_parts[0]
                file_type = name_parts[1]
                return self.rename_file(item_uuid, plain_name, file_type)
            else:
                return self.rename_file(item_uuid, new_name)
        except:
            # If file rename fails, try as folder
            return self.rename_folder(item_uuid, new_name)

    def trash_item(self, item_uuid: str) -> Dict[str, Any]:
        """WebDAV compatibility: Trash file or folder"""
        # Try as file first
        try:
            return self.trash_file(item_uuid)
        except:
            # If file trash fails, try as folder  
            return self.trash_folder(item_uuid)

    def update_file(self, file_uuid: str, new_file_id: str, new_size: int) -> Dict[str, Any]:
        """WebDAV compatibility: Update file content"""
        payload = {
            'fileId': new_file_id,
            'size': new_size
        }
        return self.replace_file(file_uuid, payload)


# Global instance
api_client = ApiClient()