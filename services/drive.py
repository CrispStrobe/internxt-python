#!/usr/bin/env python3
"""
internxt_cli/services/drive.py
with path resolution
"""

import os
import sys
import hashlib
import fnmatch
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from tqdm import tqdm
import time
import uuid
import threading

try:
    from ..config.config import config_service
    from ..utils.api import api_client
    from .crypto import crypto_service
    from .auth import auth_service
except (ImportError, ValueError):
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from config.config import config_service
    from utils.api import api_client
    from services.crypto import crypto_service
    from services.auth import auth_service


class DriveService:
    """
    Extended Drive operations with path resolution and trash operations
    """

    def __init__(self):
        self.config = config_service
        self.api = api_client
        self.crypto = crypto_service
        self.auth = auth_service

        self.folder_content_cache = {}
        self.cache_lock = threading.Lock()
        # Cache TTL for folder content listings. Bumped from 10m to 1h so that
        # long-running batch uploads (where we walk a remote tree once at the
        # start and then upload thousands of files) don't re-list folders.
        self.CACHE_DURATION_SECONDS = 3600  # 1 hour

        self.TWENTY_GIGABYTES = 20 * 1024 * 1024 * 1024   # 20GB limit
        self.MULTIPART_THRESHOLD = 100 * 1024 * 1024      # 100MB multipart threshold
        self.CHUNK_SIZE = 64 * 1024 * 1024                # 64MB chunks

        # Memory-gated concurrency: only allow as many simultaneous
        # read+encrypt operations as fit in available RAM.  The semaphore
        # value is computed lazily per-file based on current free memory.
        self._mem_lock = threading.Lock()      # protects _mem_reserved
        self._mem_reserved = 0                 # bytes currently claimed
        self._mem_cond = threading.Condition(self._mem_lock)

    @staticmethod
    def _available_memory() -> int:
        """Return available RAM in bytes (best-effort, cross-platform)."""
        try:
            import psutil
            return psutil.virtual_memory().available
        except ImportError:
            pass
        # Platform-specific fallbacks (no psutil)
        try:
            if sys.platform == 'darwin':
                import subprocess
                ps = int(subprocess.check_output(['sysctl', '-n', 'hw.pagesize']).strip())
                vm = subprocess.check_output(['vm_stat']).decode()
                free = spec = 0
                for line in vm.splitlines():
                    if 'Pages free' in line:
                        free = int(line.split(':')[1].strip().rstrip('.'))
                    elif 'Pages speculative' in line:
                        spec = int(line.split(':')[1].strip().rstrip('.'))
                return (free + spec) * ps
            elif sys.platform == 'win32':
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                return stat.ullAvailPhys
            else:
                # Linux / other Unix: read from /proc/meminfo
                with open('/proc/meminfo') as f:
                    for line in f:
                        if line.startswith('MemAvailable:'):
                            return int(line.split()[1]) * 1024  # kB -> bytes
        except Exception:
            pass
        # Last resort: assume 4 GB available
        return 4 * 1024 * 1024 * 1024

    def _mem_acquire(self, need: int) -> None:
        """Block until *need* bytes can be reserved without exceeding available RAM.

        We keep a safety margin of 1 GB so the rest of the process (and OS)
        still has breathing room.  If no other reservation is active AND
        available memory is too low, we still let one worker through so that
        progress is never deadlocked (the OS may reclaim caches/buffers).
        """
        SAFETY_MARGIN = 1 * 1024 * 1024 * 1024  # 1 GB

        with self._mem_cond:
            while True:
                avail = self._available_memory()
                headroom = max(0, avail - SAFETY_MARGIN)
                if need <= headroom - self._mem_reserved:
                    # Enough real memory for this reservation
                    self._mem_reserved += need
                    return
                if self._mem_reserved == 0:
                    # Nothing else reserved — let one through to avoid deadlock,
                    # even if the OS reports tight memory (caches may be reclaimable).
                    self._mem_reserved += need
                    return
                # Wait for another worker to release memory
                self._mem_cond.wait(timeout=5)  # re-check every 5s

    def _mem_release(self, amount: int) -> None:
        """Return a previous reservation."""
        with self._mem_cond:
            self._mem_reserved = max(0, self._mem_reserved - amount)
            self._mem_cond.notify_all()

    def _get_network_auth(self, user_creds: Dict[str, Any]) -> tuple:
        """Creates Basic Auth for Network API"""
        bridge_user = user_creds.get('bridgeUser')
        user_id = user_creds.get('userId')
        if not bridge_user or not user_id:
            raise ValueError("Missing network credentials")
        
        hashed_password = hashlib.sha256(str(user_id).encode()).hexdigest()
        return (bridge_user, hashed_password)

    # ========== PATH RESOLUTION ==========

    def resolve_path(self, path: str) -> Dict[str, Any]:
        """
        Resolve a path to UUID and metadata
        Returns: {'type': 'file'/'folder', 'uuid': str, 'metadata': dict, 'path': str}
        """
        credentials = self.auth.get_auth_details()
        root_folder_uuid = credentials['user'].get('rootFolderId', '')
        
        path = path.strip()

        if path == '.':
            path = '/'

        if path.startswith('/'):
            path = path[1:]
        
        if not path:
            return {
                'type': 'folder', 'uuid': root_folder_uuid,
                'metadata': {'uuid': root_folder_uuid, 'plainName': 'Root'},
                'path': '/'
            }
        
        path_parts = [part for part in path.split('/') if part]
        current_folder_uuid = root_folder_uuid
        resolved_path_parts = []
        
        for i, part in enumerate(path_parts):
            is_last_part = (i == len(path_parts) - 1)
            folder_content = self.get_folder_content(current_folder_uuid)
            
            # Look for folder
            found_folder = None
            for folder in folder_content['folders']:
                if folder.get('plainName') == part or folder.get('name') == part:
                    found_folder = folder
                    break
            
            # Look for file (only if last part)
            found_file = None
            if is_last_part:
                for file in folder_content['files']:
                    file_name = file.get('plainName', '')
                    file_type = file.get('type', '')
                    full_name = f"{file_name}.{file_type}" if file_type else file_name
                    
                    if (file_name == part or full_name == part or file.get('name') == part):
                        found_file = file
                        break
            
            if found_folder and (not is_last_part or not found_file):
                resolved_path_parts.append(found_folder.get('plainName', part))
                current_folder_uuid = found_folder['uuid']
                
                if is_last_part:
                    return {
                        'type': 'folder', 'uuid': found_folder['uuid'],
                        'metadata': found_folder, 'path': '/' + '/'.join(resolved_path_parts)
                    }
                    
            elif found_file and is_last_part:
                file_name = found_file.get('plainName', '')
                file_type = found_file.get('type', '')
                full_name = f"{file_name}.{file_type}" if file_type else file_name
                resolved_path_parts.append(full_name)
                
                return {
                    'type': 'file', 'uuid': found_file['uuid'],
                    'metadata': found_file, 'path': '/' + '/'.join(resolved_path_parts)
                }
            else:
                current_path = '/' + '/'.join(resolved_path_parts + [part])
                raise FileNotFoundError(f"Path not found: {current_path}")
        
        return {
            'type': 'folder', 'uuid': current_folder_uuid,
            'metadata': {'uuid': current_folder_uuid, 'plainName': path_parts[-1] if path_parts else 'Root'},
            'path': '/' + '/'.join(resolved_path_parts)
        }

    def download_file_by_path(self, file_path: str, destination_path_str: Optional[str] = None):
        """Download file by path instead of UUID"""
        print(f"🔍 Resolving path: {file_path}")
        
        resolved = self.resolve_path(file_path)
        if resolved['type'] != 'file':
            raise ValueError(f"Path '{file_path}' is a folder, not a file")
        
        file_uuid = resolved['uuid']
        print(f"📋 Resolved to file UUID: {file_uuid}")
        
        if not destination_path_str:
            filename = Path(resolved['path']).name
            destination_path_str = f"./{filename}"
        
        return self.download_file(file_uuid, destination_path_str)

    def list_folder_with_paths(self, folder_path: str = "/") -> Dict[str, List[Dict[str, Any]]]:
        """List folder contents with full paths"""
        print(f"📁 Listing folder: {folder_path}")
        
        if folder_path == "" or folder_path == "/":
            resolved = self.resolve_path("/")
        else:
            resolved = self.resolve_path(folder_path)
        
        if resolved['type'] != 'folder':
            raise ValueError(f"Path '{folder_path}' is a file, not a folder")
        
        folder_uuid = resolved['uuid']
        base_path = resolved['path']
        content = self.get_folder_content(folder_uuid)
        
        # Enhance with path info
        enhanced_folders = []
        for folder in content['folders']:
            folder_name = folder.get('plainName', folder.get('name', 'Unknown'))
            full_path = f"{base_path.rstrip('/')}/{folder_name}"
            
            enhanced_folders.append({
                **folder,
                'path': full_path,
                'display_name': folder_name,
                'size_display': '<DIR>',
                'modified': folder.get('modificationTime') or folder.get('updatedAt', ''),
            })
        
        enhanced_files = []
        for file in content['files']:
            file_name = file.get('plainName', '')
            file_type = file.get('type', '')
            display_name = f"{file_name}.{file_type}" if file_type else file_name
            full_path = f"{base_path.rstrip('/')}/{display_name}"
            
            # FIXED: Convert size string from API to integer before formatting
            try:
                size_bytes = int(file.get('size', 0))
            except (ValueError, TypeError):
                size_bytes = 0
            size_display = self._format_size(size_bytes)
            
            enhanced_files.append({
                **file,
                'path': full_path,
                'display_name': display_name,
                'size_display': size_display,
                'modified': file.get('modificationTime') or file.get('updatedAt', ''),
            })
        
        return {
            'folders': enhanced_folders,
            'files': enhanced_files,
            'current_path': base_path
        }

    def find_files(self, search_term: str, folder_path: str = "/", case_sensitive: bool = False, max_depth: int = -1) -> List[Dict[str, Any]]:
        """
        Search for files by name with wildcards, with optional case sensitivity and max depth.
        max_depth = -1 means infinite depth.
        max_depth = 1 means search *only* this folder, not subfolders.
        """
        if case_sensitive:
            print(f"🔍 Searching for '{search_term}' (case-sensitive) in {folder_path}")
        else:
            print(f"🔍 Searching for '{search_term}' (case-insensitive) in {folder_path}")
        
        results = []
        
        def search_recursive(current_path: str, current_relative_depth: int):
            # Check if we have gone too deep
            # max_depth=1 will stop recursion (depth 0 >= 1 is false)
            # max_depth=2 will allow one level (depth 1 >= 2 is false)
            if max_depth != -1 and current_relative_depth >= max_depth:
                return # Stop searching this branch
        
            try:
                # This call is cached, so it's fast
                content = self.list_folder_with_paths(current_path)
                
                # Check files in current folder
                for file in content['files']:
                    display_name = file['display_name']
                    
                    match = False
                    if case_sensitive:
                        match = fnmatch.fnmatch(display_name, search_term)
                    else:
                        match = fnmatch.fnmatch(display_name.lower(), search_term.lower())
                    
                    if match:
                        results.append({**file, 'found_in': current_path})

                # Search subfolders recursively
                for folder in content['folders']:
                    search_recursive(folder['path'], current_relative_depth + 1)
                    
            except Exception as e:
                print(f"   ⚠️  Could not search in {current_path}: {e}")
        
        search_recursive(folder_path, 0)
        
        print(f"📍 Found {len(results)} matching files")
        return results

    # ========== TRASH OPERATIONS ==========

    def trash_file(self, file_uuid: str) -> Dict[str, Any]:
        """Move file to trash"""
        try:
            self._clear_parent_cache_for_item(file_uuid, 'file')
            response = self.api.trash_file(file_uuid)  # Uses corrected bulk API
            return {'success': True, 'message': 'File moved to trash successfully', 'file': {'uuid': file_uuid}, 'result': response}
        except Exception as e:
            raise Exception(f"Failed to trash file: {e}")

    def trash_folder(self, folder_uuid: str) -> Dict[str, Any]:
        """Move folder to trash"""
        try:
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            response = self.api.trash_folder(folder_uuid)  # Uses corrected bulk API
            return {'success': True, 'message': 'Folder moved to trash successfully', 'folder': {'uuid': folder_uuid}, 'result': response}
        except Exception as e:
            raise Exception(f"Failed to trash folder: {e}")

    def trash_by_path(self, path: str) -> Dict[str, Any]:
        """Move file or folder to trash by path"""
        print(f"🗑️  Moving to trash: {path}")
        
        resolved = self.resolve_path(path)
        
        if resolved['type'] == 'file':
            return self.trash_file(resolved['uuid'])
        else:
            return self.trash_folder(resolved['uuid'])

    def delete_permanently_file(self, file_uuid: str) -> Dict[str, Any]:
        """Permanently delete file"""
        try:
            self._clear_parent_cache_for_item(file_uuid, 'file')
            self.api.delete_file(file_uuid)
            return {'success': True, 'message': 'File permanently deleted successfully'}
        except Exception as e:
            raise Exception(f"Failed to permanently delete file: {e}") from e

    def delete_permanently_folder(self, folder_uuid: str) -> Dict[str, Any]:
        """Permanently delete folder"""
        try:
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            self.api.delete_folder(folder_uuid)
            return {'success': True, 'message': 'Folder permanently deleted successfully'}
        except Exception as e:
            raise Exception(f"Failed to permanently delete folder: {e}") from e

    def delete_permanently_by_path(self, path: str) -> Dict[str, Any]:
        """Permanently delete file or folder by path"""
        print(f"🗑️  Permanently deleting: {path}")
        
        resolved = self.resolve_path(path)
        
        if resolved['type'] == 'file':
            return self.delete_permanently_file(resolved['uuid'])
        else:
            return self.delete_permanently_folder(resolved['uuid'])

    # ========== MOVE AND RENAME OPERATIONS ==========

    def move_file(self, file_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Move file to different folder"""
        try:
            self._clear_parent_cache_for_item(file_uuid, 'file')
            response = self.api.move_file(file_uuid, destination_folder_uuid)
            with self.cache_lock:
                self.folder_content_cache.pop(destination_folder_uuid, None)
            return {'success': True, 'message': f'File moved successfully to: {destination_folder_uuid}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to move file: {e}")

    def move_folder(self, folder_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Move folder to different folder"""
        try:
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            response = self.api.move_folder(folder_uuid, destination_folder_uuid)
            with self.cache_lock:
                self.folder_content_cache.pop(destination_folder_uuid, None)
            return {'success': True, 'message': f'Folder moved successfully to: {destination_folder_uuid}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to move folder: {e}")

    def rename_file(self, file_uuid: str, new_name: str) -> Dict[str, Any]:
        """Rename file"""
        try:
            # Parse name and extension
            if '.' in new_name:
                name_parts = new_name.rsplit('.', 1)
                plain_name = name_parts[0]
                file_type = name_parts[1]
            else:
                plain_name = new_name
                file_type = None
                
            response = self.api.rename_file(file_uuid, plain_name, file_type)

            self._clear_parent_cache_for_item(file_uuid, 'file')

            return {'success': True, 'message': f'File renamed successfully to: {new_name}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to rename file: {e}")

    def rename_folder(self, folder_uuid: str, new_name: str) -> Dict[str, Any]:
        """Rename folder"""
        try:
            response = self.api.rename_folder(folder_uuid, new_name)
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            return {'success': True, 'message': f'Folder renamed successfully to: {new_name}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to rename folder: {e}")
        
    def move_item(self, item_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Move file or folder to different folder (WebDAV required)"""
        try:
            # Try as file first
            try:
                return self.move_file(item_uuid, destination_folder_uuid)
            except Exception:
                # If file move fails, try as folder
                return self.move_folder(item_uuid, destination_folder_uuid)
        except Exception as e:
            raise Exception(f"Failed to move item {item_uuid}: {e}") from e

    def rename_item(self, item_uuid: str, new_name: str) -> Dict[str, Any]:
        """Rename file or folder (WebDAV required)"""
        try:
            # Try as file first
            try:
                return self.rename_file(item_uuid, new_name)
            except Exception:
                # If file rename fails, try as folder
                return self.rename_folder(item_uuid, new_name)
        except Exception as e:
            raise Exception(f"Failed to rename item {item_uuid}: {e}") from e

    def trash_item(self, item_uuid: str) -> Dict[str, Any]:
        """Move file or folder to trash (WebDAV required)"""
        try:
            # Use the corrected API trash methods
            try:
                return self.api.trash_file(item_uuid)
            except Exception:
                return self.api.trash_folder(item_uuid)
        except Exception as e:
            raise Exception(f"Failed to trash item {item_uuid}: {e}") from e
        
    def upload_with_safety_pattern(self, local_path: Path, remote_folder_uuid: str, filename: str):
        """
        Safe Upload Flow:
        1. Rename existing file to .backup
        2. Upload new file
        3. If success: delete .backup
        4. If fail: rename .backup back to original
        """
        # Check if file exists
        full_path = f"/{filename}" # Simplified for example
        existing_item = None
        try:
            existing_item = self.resolve_path(full_path)
        except FileNotFoundError:
            pass

        backup_uuid = None
        orig_name = filename
        
        if existing_item and existing_item['type'] == 'file':
            backup_name = f"{filename}.bak-{uuid.uuid4().hex[:6]}"
            print(f"⚠️ DEBUG: File conflict. Creating safety backup: {backup_name}")
            self.api.rename_file(existing_item['uuid'], backup_name)
            backup_uuid = existing_item['uuid']

        try:
            # Perform actual upload
            print(f"📤 DEBUG: Uploading {filename} to {remote_folder_uuid}...")
            new_file = self.upload_file_to_folder(str(local_path), remote_folder_uuid)
            
            # Success: Cleanup backup
            if backup_uuid:
                print(f"🗑️ DEBUG: Upload successful. Purging backup {backup_uuid}")
                self.api.delete_permanently(backup_uuid, "file")
            return new_file

        except Exception as e:
            # Failure: Rollback
            if backup_uuid:
                print(f"🚨 DEBUG: Upload FAILED. Rolling back backup to {orig_name}")
                self.api.rename_file(backup_uuid, orig_name)
            raise e

    def update_file(self, file_uuid: str, local_path: str) -> Dict[str, Any]:
        """Update existing file with new content (WebDAV required for PUT operations)"""
        try:
            # Get current file metadata
            current_metadata = self.api.get_file_metadata(file_uuid)
            plain_name = current_metadata.get('plainName', '')

            # Upload new content and get new file ID
            file_path = Path(local_path)
            file_size = file_path.stat().st_size
            
            # Get credentials and upload new version
            credentials = self.auth.get_auth_details()
            user = credentials['user']
            bucket_id = user['bucket']
            mnemonic = user['mnemonic']
            network_auth = self._get_network_auth(user)
            
            with open(file_path, 'rb') as f:
                plaintext = f.read()
            
            # Encrypt and upload
            encrypted_data, file_index_hex = self.crypto.encrypt_stream_internxt_protocol(plaintext, mnemonic, bucket_id)
            start_response = self.api.start_upload(bucket_id, len(encrypted_data), auth=network_auth)
            upload_details = start_response['uploads'][0]
            upload_url = upload_details['url']
            file_network_uuid = upload_details['uuid']
            
            self.api.upload_chunk(upload_url, encrypted_data)
            
            encrypted_hash = hashlib.sha256(encrypted_data).hexdigest()
            finish_payload = {
                'index': file_index_hex,
                'shards': [{'hash': encrypted_hash, 'uuid': file_network_uuid}]
            }
            finish_response = self.api.finish_upload(bucket_id, finish_payload, auth=network_auth)
            network_file_id = finish_response['id']
            
            # Replace file content using corrected API
            replace_payload = {
                'fileId': network_file_id,
                'size': file_size
            }
            result = self.api.replace_file(file_uuid, replace_payload)
            self._clear_parent_cache_for_item(file_uuid, 'file')
            
            return {
                'success': True,
                'message': f'File {plain_name} updated successfully',
                'result': result
            }
            
        except Exception as e:
            raise Exception(f"Failed to update file {file_uuid}: {e}")
        
    def check_file_exists(self, path: str) -> Optional[Dict[str, Any]]:
        """
        Check if a file exists at the given path without throwing an exception.
        Returns file info if exists, None otherwise.
        """
        try:
            resolved = self.resolve_path(path)
            return resolved if resolved['type'] == 'file' else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def check_folder_exists(self, path: str) -> Optional[Dict[str, Any]]:
        """
        Check if a folder exists at the given path without throwing an exception.
        Returns folder info if exists, None otherwise.
        """
        try:
            resolved = self.resolve_path(path)
            return resolved if resolved['type'] == 'folder' else None
        except FileNotFoundError:
            return None
        except Exception:
            return None
        
    def move_by_path(self, source_path: str, target_folder_path: str) -> Dict[str, Any]:
        """
        Robustly moves an item from one path to another.
        High debug verbosity for tracing resolution errors.
        """
        print(f"🚚 TRACE: Attempting to move '{source_path}' to '{target_folder_path}'")
        
        # 1. Resolve the Source (Can be file or folder)
        source = self.resolve_path(source_path)
        source_uuid = source['uuid']
        source_type = source['type']
        print(f"🔍 TRACE: Source resolved. Type: {source_type.upper()}, UUID: {source_uuid}")

        # 2. Resolve the Target Folder
        target = self.resolve_path(target_folder_path)
        if target['type'] != 'folder':
            raise ValueError(f"Target '{target_folder_path}' is a file. You can only move items into folders.")
        
        target_uuid = target['uuid']
        print(f"🎯 TRACE: Target folder resolved. UUID: {target_uuid}")

        # 3. Perform the move based on type
        try:
            if source_type == 'file':
                result = self.api.move_file(source_uuid, target_uuid)
            else:
                result = self.api.move_folder(source_uuid, target_uuid)
            
            # 4. Cache Management: Clear parent caches so the UI updates
            with self.cache_lock:
                self.folder_content_cache.pop(target_uuid, None)
                print(f"🧹 TRACE: Cleared cache for target folder: {target_uuid}")
            
            print("✅ TRACE: Move successful!")
            return result

        except Exception as e:
            print(f"❌ TRACE: Move failed: {str(e)}")
            raise

    def copy_item(self, item_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Copy file to different folder preserving timestamps"""
        try:
            # Get file metadata
            metadata = self.api.get_file_metadata(item_uuid)
            
            # Extract timestamps - try both field name variations
            creation_time = metadata.get('creationTime') or metadata.get('createdAt')
            modification_time = metadata.get('modificationTime') or metadata.get('updatedAt')
            
            # Download file to temporary location
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_path = temp_file.name
            
            try:
                self.download_file(item_uuid, temp_path)
                
                # Upload to new location WITH timestamps
                plain_name = metadata.get('plainName', '')
                file_type = metadata.get('type', '')
                
                print("     📋 Copying with timestamp preservation:")
                if creation_time:
                    print(f"        Original creation: {creation_time}")
                if modification_time:
                    print(f"        Original modification: {modification_time}")
                
                # Create new file with upload_file_to_folder, passing timestamps
                result = self.upload_file_to_folder(
                    temp_path, 
                    destination_folder_uuid, 
                    plain_name, 
                    file_type,
                    creation_time=creation_time,
                    modification_time=modification_time
                )
                
                return {
                    'success': True,
                    'message': f'File {plain_name} copied successfully',
                    'result': result,
                    'timestamps_preserved': bool(creation_time or modification_time)
                }
                
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        except Exception as e:
            raise Exception(f"Copy failed: {e}") from e
        
    def create_upload_checkpoint(self, file_path: Path, target_uuid: str) -> str:
        """
        Create a checkpoint file for resumable uploads.
        Returns checkpoint file path.
        """
        checkpoint_dir = self.config.internxt_cli_data_dir / 'upload_checkpoints'
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        # Non-cryptographic ID for checkpoint file naming.
        checkpoint_id = hashlib.sha256(f"{file_path}{target_uuid}".encode()).hexdigest()[:32]
        checkpoint_file = checkpoint_dir / f"{checkpoint_id}.json"
        
        checkpoint_data = {
            'file_path': str(file_path),
            'target_uuid': target_uuid,
            'file_size': file_path.stat().st_size,
            'timestamp': time.time(),
            'status': 'started'
        }
        
        import json
        with open(checkpoint_file, 'w') as f:
            json.dump(checkpoint_data, f)
        
        return str(checkpoint_file)

    def remove_upload_checkpoint(self, checkpoint_file: str):
        """Remove upload checkpoint after successful upload."""
        try:
            Path(checkpoint_file).unlink()
        except Exception:
            pass
        
    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename for upload, removing or replacing problematic characters.
        """
        # Remove or replace characters that might cause issues
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        
        # Remove leading/trailing spaces and dots
        filename = filename.strip(' .')
        
        # Ensure filename is not empty
        if not filename:
            filename = 'unnamed_file'
        
        return filename

    def upload_file_to_folder(self, file_path_str: str, destination_folder_uuid: str,
                            custom_name: Optional[str] = None, custom_extension: Optional[str] = None,
                            creation_time: Optional[str] = None, modification_time: Optional[str] = None):
        """Upload file with custom name/extension and optional timestamps to specific folder"""
        credentials = self.auth.get_auth_details()
        user = credentials['user']
        bucket_id = user['bucket']
        mnemonic = user['mnemonic']
        network_auth = self._get_network_auth(user)

        file_path = Path(file_path_str)
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found at: {file_path}")

        file_size = file_path.stat().st_size
        if file_size > self.TWENTY_GIGABYTES:
            raise ValueError(f"File is too large (must be less than {self._format_size(self.TWENTY_GIGABYTES)})")
        
        # Use custom name/extension if provided
        file_name = custom_name or file_path.stem
        file_extension = custom_extension if custom_extension is not None else file_path.suffix.lstrip('.')
        
        display_name = f"{file_name}.{file_extension}" if file_extension else file_name
        print(f"     📤 Uploading '{display_name}' ({self._format_size(file_size)})...")
        
        # Calculate dynamic timeout based on file size (assume 100 KB/s minimum speed)
        min_speed_kbps = 100  # 100 KB/s
        timeout_seconds = max(300, int(file_size / (min_speed_kbps * 1024)) + 60)
        print(f"     ⏱️  Upload timeout: {timeout_seconds}s (~{timeout_seconds//60} minutes)")
        
        # Warn for very large files
        if file_size > 500 * 1024 * 1024:  # > 500MB
            print("     ⚠️  Large file detected - encryption may take several minutes")
            print("     💡 Please be patient, progress will be shown...")
        
        # Log timestamp preservation attempt
        if creation_time or modification_time:
            print("     🕐 Attempting to preserve timestamps:")
            if creation_time:
                print(f"        Creation: {creation_time}")
            if modification_time:
                print(f"        Modification: {modification_time}")
        
        # Memory-gated: reserve ~2x file_size (plaintext + encrypted copy)
        # so we don't OOM when multiple workers handle large files at once.
        # After encryption we del plaintext and release half; the other half
        # stays reserved until the upload finishes and encrypted_data is freed.
        mem_need = file_size * 2
        mem_held = 0  # tracks how much we currently hold
        avail = self._available_memory()
        print(f"     💾 Memory: {self._format_size(avail)} available, need ~{self._format_size(mem_need)} for read+encrypt")
        self._mem_acquire(mem_need)
        mem_held = mem_need

        try:
            print("     📖 Reading file from disk...")
            start_read = time.time()
            try:
                with open(file_path, 'rb') as f:
                    plaintext = f.read()
            except Exception as e:
                raise IOError(f"Failed to read file {file_path}: {e}")

            read_time = time.time() - start_read
            print(f"     ✅ File read complete ({read_time:.1f}s, {self._format_size(len(plaintext))})")

            # Step 1: Encryption
            print(f"\n     🔐 Step 1/5: Encrypting {self._format_size(len(plaintext))}...")
            print("        This may take a few minutes for large files...")
            start_encrypt = time.time()

            encrypted_data, file_index_hex = self.crypto.encrypt_stream_internxt_protocol(
                plaintext, mnemonic, bucket_id
            )

            encrypt_time = time.time() - start_encrypt
            plaintext_len = len(plaintext)
            del plaintext  # free ~file_size of RAM immediately
            # Release half — plaintext is gone, encrypted_data (~file_size) stays
            self._mem_release(file_size)
            mem_held = file_size

            print("     ✅ Encryption complete!")
            print(f"        Time: {encrypt_time:.1f}s ({self._format_size(int(plaintext_len/encrypt_time))}/s)")
            print(f"        Encrypted size: {self._format_size(len(encrypted_data))}")
            print(f"        File index: {file_index_hex[:16]}...")
        except BaseException:
            self._mem_release(mem_held)
            mem_held = 0
            raise

        # encrypted_data is in memory (~file_size); file_size is still reserved.
        # Release the remaining reservation when we're done (success or final failure).
        try:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # Step 2: Request upload URL
                    print("\n     🚀 Step 2/5: Requesting upload URL from server...")
                    start_init = time.time()

                    start_response = self.api.start_upload(bucket_id, len(encrypted_data), auth=network_auth)
                    upload_details = start_response['uploads'][0]
                    upload_url = upload_details['url']
                    file_network_uuid = upload_details['uuid']

                    init_time = time.time() - start_init
                    print(f"     ✅ Upload URL received ({init_time:.1f}s)")
                    print(f"        Network UUID: {file_network_uuid}")
                    print(f"        Upload URL: {upload_url[:50]}...")

                    # Step 3: Upload encrypted data
                    print(f"\n     ☁️  Step 3/5: Uploading {self._format_size(len(encrypted_data))} to network...")
                    print(f"        Timeout: {timeout_seconds}s")
                    print("        This is the longest step - please be patient...")

                    start_upload = time.time()
                    self._upload_chunk_with_progress(upload_url, encrypted_data, timeout_seconds)
                    upload_time = time.time() - start_upload

                    upload_speed = len(encrypted_data) / upload_time if upload_time > 0 else 0
                    print("     ✅ Upload complete!")
                    print(f"        Time: {upload_time:.1f}s ({self._format_size(int(upload_speed))}/s)")

                    # Step 4: Finalize upload
                    print("\n     ✅ Step 4/5: Finalizing upload on server...")
                    start_finalize = time.time()

                    encrypted_hash = hashlib.sha256(encrypted_data).hexdigest()
                    print(f"        Computed hash: {encrypted_hash[:16]}...")

                    finish_payload = {
                        'index': file_index_hex,
                        'shards': [{'hash': encrypted_hash, 'uuid': file_network_uuid}]
                    }
                    finish_response = self.api.finish_upload(bucket_id, finish_payload, auth=network_auth)
                    network_file_id = finish_response['id']

                    finalize_time = time.time() - start_finalize
                    print(f"     ✅ Server finalization complete ({finalize_time:.1f}s)")
                    print(f"        Network file ID: {network_file_id}")

                    # Step 5: Create file entry
                    print("\n     📋 Step 5/5: Creating file entry in your Drive...")
                    start_entry = time.time()

                    file_entry_payload = {
                        'folderUuid': destination_folder_uuid,
                        'plainName': file_name,
                        'type': file_extension if file_extension else '',
                        'size': file_size,
                        'bucket': bucket_id,
                        'fileId': network_file_id,
                        'encryptVersion': 'Aes03',
                        'name': ''
                    }

                    if creation_time:
                        file_entry_payload['creationTime'] = creation_time
                    if modification_time:
                        file_entry_payload['modificationTime'] = modification_time

                    created_file = self.api.create_file_entry(file_entry_payload)

                    entry_time = time.time() - start_entry
                    print(f"     ✅ File entry created ({entry_time:.1f}s)")
                    print(f"        File UUID: {created_file.get('uuid', 'N/A')}")

                    with self.cache_lock:
                        cached_item = self.folder_content_cache.get(destination_folder_uuid)
                        if cached_item:
                            cache_time, content = cached_item
                            content['files'].append(created_file)
                            self.folder_content_cache[destination_folder_uuid] = (cache_time, content)

                    if creation_time or modification_time:
                        returned_creation = created_file.get('creationTime') or created_file.get('createdAt')
                        returned_modification = created_file.get('modificationTime') or created_file.get('updatedAt')

                        if creation_time and returned_creation:
                            print(f"     ✅ Creation timestamp preserved: {returned_creation}")
                        elif creation_time:
                            print(f"     ⚠️  Creation timestamp NOT set (API returned: {returned_creation})")

                        if modification_time and returned_modification:
                            print(f"     ✅ Modification timestamp preserved: {returned_modification}")
                        elif modification_time:
                            print(f"     ⚠️  Modification timestamp NOT set (API returned: {returned_modification})")

                    total_time = time.time() - start_read
                    print("\n     🎉 Upload complete!")
                    print(f"        Total time: {total_time:.1f}s ({total_time/60:.1f} minutes)")
                    print(f"        Average speed: {self._format_size(int(file_size/total_time))}/s")

                    return created_file

                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        print(f"\n     ⚠️  Upload failed (attempt {attempt+1}/{max_retries}): {e}")
                        print(f"     ⏳ Retrying in {wait_time} seconds...")
                        time.sleep(wait_time)
                    else:
                        raise Exception(f"Upload failed after {max_retries} attempts: {e}")
        finally:
            # Release the remaining reservation for encrypted_data
            if mem_held > 0:
                self._mem_release(mem_held)

    def _upload_chunk_with_progress(self, upload_url: str, chunk_data: bytes, timeout_seconds: int):
        """Upload chunk with custom timeout and progress tracking"""
        import requests
        
        chunk_size_mb = len(chunk_data) / (1024 * 1024)
        print(f"        Starting upload of {chunk_size_mb:.1f} MB...")
        
        # Create a custom session with longer timeout
        session = requests.Session()
        
        # For large uploads, show progress
        if len(chunk_data) > 10 * 1024 * 1024:  # > 10MB
            with tqdm(
                total=len(chunk_data),
                unit='B',
                unit_scale=True,
                desc='        Progress',
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{rate_fmt}] ETA: {remaining}',
                leave=False
            ) as pbar:
                
                # Split into smaller chunks for progress tracking
                upload_chunk_size = 1024 * 1024  # 1MB chunks for progress
                uploaded = 0
                
                # Use streaming upload
                def data_generator():
                    nonlocal uploaded
                    while uploaded < len(chunk_data):
                        chunk = chunk_data[uploaded:uploaded + upload_chunk_size]
                        uploaded += len(chunk)
                        pbar.update(len(chunk))
                        yield chunk
                
                response = session.put(
                    upload_url,
                    data=data_generator(),
                    headers={'Content-Type': 'application/octet-stream'},
                    timeout=timeout_seconds
                )
                response.raise_for_status()
        else:
            # Small file - direct upload
            response = session.put(
                upload_url,
                data=chunk_data,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=timeout_seconds
            )
            response.raise_for_status()
        
        print(f"        Upload request completed (status: {response.status_code})")

    # ========== CORE OPERATIONS ==========

    def get_folder_content(self, folder_uuid: str) -> Dict[str, List[Dict[str, Any]]]:
        """Get folder contents, with caching"""
        
        # --- BEGIN CACHE CHECK ---
        with self.cache_lock:
            cached_item = self.folder_content_cache.get(folder_uuid)
            if cached_item:
                cache_time, content = cached_item
                if (time.time() - cache_time) < self.CACHE_DURATION_SECONDS:
                    # Return the cached content if it's not expired
                    return content
        # --- END CACHE CHECK ---

        try:
            self.auth.get_auth_details()  # ensures session is initialized
            folders = self._get_all_folders(folder_uuid)
            files = self._get_all_files(folder_uuid)
            content = {'folders': folders, 'files': files}

            # --- BEGIN CACHE SET ---
            with self.cache_lock:
                # Store the new content with the current time
                self.folder_content_cache[folder_uuid] = (time.time(), content)
            # --- END CACHE SET ---
            
            return content
        except Exception as e:
            print(f"Error getting folder content: {e}")
            return {'folders': [], 'files': []}
        
    def _clear_parent_cache_for_item(self, item_uuid: str, item_type: str = 'file'):
        """Helper to find an item's parent and clear its cache."""
        parent_uuid = None
        try:
            if item_type == 'file':
                metadata = self.api.get_file_metadata(item_uuid)
                parent_uuid = metadata.get('folderUuid')
            else: # 'folder'
                metadata = self.api.get_folder_metadata(item_uuid)
                parent_uuid = metadata.get('parentUuid')
            
            if parent_uuid:
                with self.cache_lock:
                    self.folder_content_cache.pop(parent_uuid, None)
                    print(f"  -> Cache cleared for parent folder: {parent_uuid}")
        except Exception as e:
            # This is not fatal, just log it
            print(f"  -> ⚠️  Could not clear parent cache for {item_uuid} (parent: {parent_uuid}): {e}")

    def list_folder(self, folder_uuid: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """List folder contents - backward compatibility"""
        credentials = self.auth.get_auth_details()

        if not folder_uuid:
            folder_uuid = credentials['user'].get('rootFolderId', '')
            if not folder_uuid:
                raise ValueError("No root folder ID found")

        return self.get_folder_content(folder_uuid)

    def _get_all_folders(self, folder_uuid: str, offset: int = 0) -> List[Dict[str, Any]]:
        """Recursively get all folders with pagination"""
        try:
            limit = 50 
            response = self.api.get_folder_folders(folder_uuid, offset, limit) 
            folders = response.get('result', response.get('folders', []))

            if len(folders) == limit: 
                folders.extend(self._get_all_folders(folder_uuid, offset + limit)) 

            return folders
        except Exception as e:
            print(f"Warning: Failed to get folders: {e}")
            return []

    def _get_all_files(self, folder_uuid: str, offset: int = 0) -> List[Dict[str, Any]]:
        """Recursively get all files with pagination"""
        try:
            limit = 50
            response = self.api.get_folder_files(folder_uuid, offset, limit) 
            files = response.get('result', response.get('files', []))

            if len(files) == limit: 
                files.extend(self._get_all_files(folder_uuid, offset + limit)) 

            return files
        except Exception as e:
            print(f"Warning: Failed to get files: {e}")
            return []
        
    def get_full_path_for_item(self, item_metadata: Dict[str, Any]) -> str:
        """
        Constructs the full, human-readable path for a file or folder
        by fetching its ancestors.
        """
        
        # Get the item's own name
        item_name = item_metadata.get('plainName', 'Unknown')
        if item_metadata.get('itemType') == 'file' and item_metadata.get('type'):
            item_name = f"{item_name}.{item_metadata['type']}"
        
        # Get the parent folder's UUID
        parent_uuid = item_metadata.get('folderUuid') # for files
        if not parent_uuid:
            parent_uuid = item_metadata.get('parentUuid') # for folders
        
        if not parent_uuid:
            # Item is in the root
            return f"/{item_name}"

        try:
            # Call the new API function
            ancestors = self.api.get_folder_ancestors(parent_uuid)
            
            # The 'ancestors' endpoint returns the list from root -> parent
            path_parts = [ancestor.get('plainName') for ancestor in ancestors]
            
            # Filter out the root folder's name (which can be 'root' or null)
            # and any other empty parts
            clean_parts = [part for part in path_parts if part and part.lower() != 'root']
            
            full_path = "/" + "/".join(clean_parts)
            
            # Add the item name itself
            return f"{full_path.rstrip('/')}/{item_name}"
        except Exception as e:
            print(f"  -> ⚠️  Could not build path for {item_name}: {e}")
            return f"/?/{item_name}" # Return a 'best guess' path
        
    def search_drive(self, search_term: str) -> List[Dict[str, Any]]:
        """
        Performs a fast, server-side fuzzy search across the entire drive.
        Note: The API only returns item names, types, and UUIDs, not full paths.
        """
        print(f"🔍 Performing server-side fuzzy search for: '{search_term}'")
        try:
            # This 'search_files' function already exists in your api.py
            results = self.api.search_files(search_term, offset=0, limit=50)
            
            # The API spec shows the data is in a 'data' key
            # If not, it might be in 'results' or the root
            items = results.get('data', results.get('results', results))
            
            if not isinstance(items, list):
                print(" -> ⚠️  Search returned an unexpected format.")
                return []
                
            return items
        except Exception as e:
            print(f" -> ❌ Search failed: {e}")
            return []

    def create_folder(self, name: str, parent_folder_uuid: Optional[str] = None,
                      creation_time: Optional[str] = None,
                      modification_time: Optional[str] = None) -> Dict[str, Any]:
        """Create new folder with optional timestamps AND update cache."""
        credentials = self.auth.get_auth_details()

        if not parent_folder_uuid:
            parent_folder_uuid = credentials['user'].get('rootFolderId', '')
            if not parent_folder_uuid:
                raise ValueError("No root folder ID found")
        
        payload = {
            'plainName': name,
            'parentFolderUuid': parent_folder_uuid
        }

        if creation_time:
            payload['creationTime'] = creation_time
            print(f"     🕐 Adding folder creationTime: {creation_time}")
        if modification_time:
            payload['modificationTime'] = modification_time
            print(f"     🕐 Adding folder modificationTime: {modification_time}")

        # 1. Create the folder via the API
        new_folder_metadata = self.api.create_folder(payload)
        
        # 2. Add the new folder to the parent's cache immediately
        with self.cache_lock:
            cached_item = self.folder_content_cache.get(parent_folder_uuid)
            if cached_item:
                cache_time, content = cached_item
                # Add new folder to the 'folders' list in the cache
                content['folders'].append(new_folder_metadata)
                # Save the updated cache content
                self.folder_content_cache[parent_folder_uuid] = (cache_time, content)
                print(f"  -> Cache updated for parent: {parent_folder_uuid}")
            # If parent isn't in cache, that's fine. It will be fetched
            # (and will include the new folder) on the next call.

        return new_folder_metadata

    def create_folder_recursive(self, path: str,
                              creation_time: Optional[str] = None,
                              modification_time: Optional[str] = None) -> Dict[str, Any]:
        """
        Ensures a folder path exists, creating intermediate folders if necessary.
        Sets timestamps *only* if the folder is being created new.
        Returns the metadata of the final folder.
        """
        credentials = self.auth.get_auth_details()
        root_folder_uuid = credentials['user'].get('rootFolderId', '')

        path = path.strip().strip('/')
        if not path:
             return {'uuid': root_folder_uuid, 'plainName': 'Root'}

        parts = [part for part in path.split('/') if part] # Clean empty parts
        current_parent_uuid = root_folder_uuid
        current_path_so_far = "/"
        final_folder_info = None

        for i, part in enumerate(parts):
            is_last_part = (i == len(parts) - 1)
            found_folder = None
            
            try:
                # 1. Check if the folder already exists
                content = self.get_folder_content(current_parent_uuid)
                for folder in content['folders']:
                    if folder.get('plainName') == part or folder.get('name') == part:
                        found_folder = folder
                        break

                if found_folder:
                    # 2. FOLDER EXISTS: We cannot update its timestamp.
                    # Just use its info and move to the next part.
                    current_parent_uuid = found_folder['uuid']
                    final_folder_info = found_folder
                    
                    if is_last_part and (creation_time or modification_time):
                        print(f"  -> ℹ️  Note: Folder '{part}' already exists. Cannot update timestamps (API limitation).")
                
                else:
                    # 3. FOLDER DOES NOT EXIST: Create it new.
                    try:
                        print(f"  -> Creating new folder: {part} in {current_path_so_far}")
                        
                        # We only pass timestamps if this is the *final* folder
                        if is_last_part:
                            print(f"  -> 🕐 Applying timestamps to new folder: {part}")
                            new_folder = self.create_folder(
                                part, 
                                current_parent_uuid,
                                creation_time=creation_time,
                                modification_time=modification_time
                            )
                        else:
                            # Create intermediate folders *without* timestamps
                            new_folder = self.api.create_folder({
                                'plainName': part, 
                                'parentFolderUuid': current_parent_uuid
                            })
                        
                        current_parent_uuid = new_folder['uuid']
                        final_folder_info = new_folder
                    
                    except Exception as e:
                        # Handle the race condition we saw before
                        if "already exists" in str(e):
                            print(f"  -> ℹ️  Folder '{part}' already exists (consistency). Resolving...")
                            try:
                                existing_folder_path = f"{current_path_so_far.rstrip('/')}/{part}"
                                resolved = self.resolve_path(existing_folder_path)
                                current_parent_uuid = resolved['uuid']
                                final_folder_info = resolved['metadata']
                            except Exception as e2:
                                raise Exception(f"Failed to create folder '{part}' and could not resolve it after: {e2}")
                        else:
                            raise e # Re-raise other errors

                current_path_so_far = f"{current_path_so_far.rstrip('/')}/{part}"
                
                # If this is the last part, return the info we found or created
                if is_last_part:
                    return final_folder_info

            except Exception as e:
                 raise Exception(f"Failed to resolve or create folder part '{part}' in '{current_path_so_far}': {e}")

        return {'uuid': root_folder_uuid, 'plainName': 'Root'}
    
    def validate_upload_sources(self, sources: List[str], recursive: bool = False) -> Tuple[List[Path], List[str]]:
        """
        Validate upload sources and return valid files and error messages.
        
        Returns: (valid_paths, error_messages)
        """
        valid_paths = []
        errors = []
        
        for source in sources:
            source_path = Path(source)
            
            # Check if source exists
            if not source_path.exists():
                errors.append(f"Source not found: {source}")
                continue
            
            # Check if readable
            if not os.access(source_path, os.R_OK):
                errors.append(f"Source not readable: {source}")
                continue
            
            # Check file size if it's a file
            if source_path.is_file():
                try:
                    size = source_path.stat().st_size
                    if size > self.TWENTY_GIGABYTES:
                        errors.append(f"File too large (>{self._format_size(self.TWENTY_GIGABYTES)}): {source}")
                        continue
                except Exception as e:
                    errors.append(f"Cannot read file {source}: {e}")
                    continue
            
            # Check if directory but recursive not enabled
            if source_path.is_dir() and not recursive:
                errors.append(f"Directory requires --recursive flag: {source}")
                continue
            
            valid_paths.append(source_path)
        
        return valid_paths, errors
    
    def get_upload_statistics(self, local_path: Path, recursive: bool = False) -> Dict[str, Any]:
        """
        Calculate statistics for an upload operation before starting.
        Useful for showing progress and estimating time.
        
        Returns: {
            'total_files': int,
            'total_size': int,
            'total_dirs': int,
            'file_list': List[Path]
        }
        """
        stats: Dict[str, Any] = {
            'total_files': 0,
            'total_size': 0,
            'total_dirs': 0,
            'file_list': []
        }
        
        if local_path.is_file():
            stats['total_files'] = 1
            stats['total_size'] = local_path.stat().st_size
            stats['file_list'] = [local_path]
        elif local_path.is_dir():
            if recursive:
                for item in local_path.rglob('*'):
                    if item.is_file():
                        try:
                            stats['total_files'] += 1
                            stats['total_size'] += item.stat().st_size
                            stats['file_list'].append(item)
                        except Exception:
                            pass  # Skip files we can't read
                    elif item.is_dir():
                        stats['total_dirs'] += 1
            else:
                # Just count direct children
                for item in local_path.iterdir():
                    if item.is_file():
                        try:
                            stats['total_files'] += 1
                            stats['total_size'] += item.stat().st_size
                            stats['file_list'].append(item)
                        except Exception:
                            pass
                    elif item.is_dir():
                        stats['total_dirs'] += 1
        
        return stats
    
    def should_include_file(self, file_path: Path, include_patterns: List[str],
                            exclude_patterns: List[str]) -> bool:
        """Check if a file should be included based on include/exclude patterns"""
        file_name = file_path.name
        
        # If include patterns specified, file must match at least one
        if include_patterns:
            matches_include = any(fnmatch.fnmatch(file_name, pattern) for pattern in include_patterns)
            if not matches_include:
                return False
        
        # If exclude patterns specified, file must not match any
        if exclude_patterns:
            matches_exclude = any(fnmatch.fnmatch(file_name, pattern) for pattern in exclude_patterns)
            if matches_exclude:
                return False
        
        return True

    def upload_single_item_with_conflict_handling(
            self,
            local_path: Path,
            target_remote_parent_path_str: str,
            target_folder_uuid: str,
            on_conflict: str,
            remote_filename: Optional[str] = None,
            creation_time: Optional[str] = None,
            modification_time: Optional[str] = None
        ) -> str:
        """
        Uploads a single local file, handling conflicts based on the specified strategy.
        
        Args:
            local_path: Path object for the local file.
            target_remote_parent_path_str: The full intended remote path of the PARENT folder.
            target_folder_uuid: The UUID of the *immediate parent* remote folder to upload into.
            on_conflict: 'skip' or 'overwrite'.
            remote_filename: If specified, use this filename instead of local_path.name.
            creation_time: ISO format timestamp for file creation (optional).
            modification_time: ISO format timestamp for file modification (optional).

        Returns:
            "uploaded", "skipped", or "error"
        """
        if not local_path.is_file():
            print(f"  -> ⚠️  Not a file, skipping: {local_path}")
            return "skipped"

        # Validate file size before proceeding
        try:
            file_size = local_path.stat().st_size
            if file_size > self.TWENTY_GIGABYTES:
                print(f"  -> ❌ File too large (>{self._format_size(self.TWENTY_GIGABYTES)}): {local_path.name}")
                return "error"
            if file_size == 0:
                print(f"  -> ⚠️  File is empty, skipping: {local_path.name}")
                return "skipped"
        except Exception as e:
            print(f"  -> ❌ Cannot read file: {e}")
            return "error"

        effective_remote_filename = remote_filename or local_path.name
        
        # Construct the full path of the potential target FILE for existence check
        full_target_remote_path = f"{target_remote_parent_path_str.rstrip('/')}/{effective_remote_filename}"
        if full_target_remote_path.startswith('//'):
            full_target_remote_path = full_target_remote_path[1:]
        if not full_target_remote_path.startswith('/'):
            full_target_remote_path = '/' + full_target_remote_path

        print(f"  -> Preparing upload: '{local_path.name}' ({self._format_size(file_size)}) to '{full_target_remote_path}'")
        
        if creation_time or modification_time:
            print("  -> 🕐 With timestamp preservation")

        existing_item_info = None
        try:
            existing_item_info = self.resolve_path(full_target_remote_path)
            print(f"  -> Target exists: {full_target_remote_path} (Type: {existing_item_info['type']})")
        except FileNotFoundError:
            print("  -> Target does not exist, proceeding with upload")
            pass
        except Exception as e:
            print(f"  -> ⚠️  Error checking target existence: {e}")

        if existing_item_info:
            if on_conflict == 'skip':
                print("  -> ⏭️  Skipping due to conflict policy (file exists)")
                return "skipped"
            elif on_conflict == 'overwrite':
                if existing_item_info['type'] == 'folder':
                    print(f"  -> ❌ Cannot overwrite folder with a file: {full_target_remote_path}")
                    return "error"
                else:
                    print("  -> 🔄 Overwriting existing file...")
                    try:
                        self.delete_permanently_by_path(full_target_remote_path)
                        print("  -> 🗑️  Deleted existing file for overwrite")
                    except Exception as del_err:
                        print(f"  -> ❌ Error deleting existing file for overwrite: {del_err}")
                        return "error"
            else:
                print(f"  -> ❌ Invalid conflict mode '{on_conflict}'")
                return "error"

        # --- Proceed with upload ---
        try:
            file_stem = Path(effective_remote_filename).stem
            file_suffix = Path(effective_remote_filename).suffix.lstrip('.')
            
            if not file_suffix and '.' not in effective_remote_filename:
                file_suffix = ''
            elif not file_suffix and '.' in effective_remote_filename:
                file_stem = effective_remote_filename
                file_suffix = ''

            # Upload with timestamps
            self.upload_file_to_folder(
                str(local_path),
                target_folder_uuid,
                custom_name=file_stem,
                custom_extension=file_suffix if file_suffix else None,
                creation_time=creation_time,
                modification_time=modification_time
            )
            print(f"  -> ✅ Successfully uploaded: {effective_remote_filename}")
            return "uploaded"
        except Exception as up_err:
            print(f"  -> ❌ Error during upload: {up_err}")
            import traceback
            traceback.print_exc()
            return "error"

    def download_file(self, file_uuid: str, destination_path_str: str, 
                    preserve_timestamps: bool = False):
        """Download and decrypt file with optional timestamp preservation"""
        credentials = self.auth.get_auth_details()
        user = credentials['user']
        mnemonic = user['mnemonic']
        network_auth = self._get_network_auth(user)
        
        print(f"📥 Downloading file UUID: {file_uuid} ...")
        
        with tqdm(total=5, desc="Downloading", unit="step", 
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]') as pbar:
            
            pbar.set_description("📋 Fetching file metadata")
            metadata = self.api.get_file_metadata(file_uuid)
            
            bucket_id = metadata['bucket']
            network_file_id = metadata['fileId']
            file_size = int(metadata['size'])
            file_name = metadata.get('plainName', 'downloaded_file')
            file_type = metadata.get('type')
            
            # Extract timestamps from metadata
            creation_time = metadata.get('creationTime') or metadata.get('createdAt')
            modification_time = metadata.get('modificationTime') or metadata.get('updatedAt')
            
            if file_type:
                file_name = f"{file_name}.{file_type}"
            
            print(f"     📄 File: {file_name}")
            print(f"     📊 Size: {self._format_size(file_size)}")
            if preserve_timestamps:
                print("     🕐 Remote timestamps:")
                if creation_time:
                    print(f"        Creation: {creation_time}")
                if modification_time:
                    print(f"        Modification: {modification_time}")
            
            pbar.update(1)
            
            pbar.set_description("🔗 Fetching download links")
            links_response = self.api.get_download_links(bucket_id, network_file_id, auth=network_auth)
            download_url = links_response['shards'][0]['url']
            file_index_hex = links_response['index']
            print("     🔗 Download URL acquired")
            pbar.update(1)
            
            pbar.set_description("☁️  Downloading encrypted data")
            encrypted_data = self.api.download_chunk(download_url)
            print(f"     ☁️  Downloaded {self._format_size(len(encrypted_data))} encrypted")
            pbar.update(1)
            
            pbar.set_description("🔐 Decrypting")
            decrypted_data = self.crypto.decrypt_stream_internxt_protocol(
                encrypted_data, mnemonic, bucket_id, file_index_hex
            )
            
            # Always trim to exact file size
            decrypted_data = decrypted_data[:file_size]
            print(f"     🔐 Decrypted {self._format_size(len(decrypted_data))}")
            pbar.update(1)
            
            destination_path = Path(destination_path_str)
            if destination_path.is_dir():
                destination_path = destination_path / file_name
            
            pbar.set_description("💾 Saving to disk")
            with open(destination_path, 'wb') as f:
                f.write(decrypted_data)
            print(f"     💾 Saved to: {destination_path}")
            pbar.update(1)
        
        # Set timestamps if requested
        if preserve_timestamps and (creation_time or modification_time):
            try:
                from datetime import datetime

                destination_path.stat()
                
                # Parse timestamps
                if modification_time:
                    try:
                        # Try parsing ISO format
                        mtime = datetime.fromisoformat(modification_time.replace('Z', '+00:00'))
                        mtime_ts = mtime.timestamp()
                        
                        # Set access and modification times
                        os.utime(destination_path, (mtime_ts, mtime_ts))
                        print(f"     🕐 Set modification time: {modification_time}")
                    except Exception as e:
                        print(f"     ⚠️  Could not set modification time: {e}")
                
                # Note: Setting creation time is platform-specific and often not supported
                if creation_time:
                    print("     ℹ️  Note: Creation time cannot be set on most systems")
                    
            except Exception as e:
                print(f"     ⚠️  Could not preserve timestamps: {e}")
        
        print(f"✅ Success! File downloaded to '{destination_path}'")
        return str(destination_path)

    def _format_size(self, size_bytes: int) -> str:
        """Convert bytes to human readable format"""
        if not size_bytes:
            return "0 B"

        size: float = float(size_bytes)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0

        return f"{size:.1f} PB"

    def get_file_metadata(self, file_uuid: str) -> Dict[str, Any]:
        """Get file metadata"""
        self.auth.get_auth_details()  # ensures session is initialized
        return self.api.get_file_metadata(file_uuid)

    def get_folder_metadata(self, folder_uuid: str) -> Dict[str, Any]:
        """Get folder metadata"""
        self.auth.get_auth_details()  # ensures session is initialized
        return self.api.get_folder_metadata(folder_uuid)

    def set_folder_timestamps(self, folder_uuid: str,
                              creation_time: Optional[str] = None,
                              modification_time: Optional[str] = None) -> Dict[str, Any]:
        """Update creation/modification timestamps on an existing folder.

        Used by the WebDAV provider's PROPPATCH handler so file managers
        (Finder, Explorer) can set folder timestamps on the remote.
        """
        if not creation_time and not modification_time:
            raise ValueError("Must provide creation_time or modification_time")
        payload: Dict[str, Any] = {}
        if creation_time:
            payload['creationTime'] = creation_time
        if modification_time:
            payload['modificationTime'] = modification_time
        result = self.api.update_folder_metadata(folder_uuid, payload)
        # Invalidate the parent-folder cache so subsequent listings see new times.
        self._clear_parent_cache_for_item(folder_uuid, 'folder')
        return result


# Global instance
drive_service = DriveService()