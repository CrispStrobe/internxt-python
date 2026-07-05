#!/usr/bin/env python3
"""
internxt_cli/services/webdav_provider.py
"""

import os
import io
import time
import tempfile
import threading
from typing import Dict, Any, List, Optional, Union

try:
    from wsgidav.dav_provider import DAVProvider, DAVCollection, DAVNonCollection
    from wsgidav.dav_error import DAVError, HTTP_NOT_FOUND, HTTP_FORBIDDEN
    import mimetypes
except ImportError:
    print("❌ Missing WebDAV dependency. Install with: pip install WsgiDAV")
    raise


# Configuration
MAX_MEMORY_SIZE = 100 * 1024 * 1024  # 100MB - files larger than this use disk
VERBOSE_LOGGING = True  # Set to True for detailed logging


class StreamingFileUpload:
    """Hybrid file upload handler - memory for small files, disk for large files"""
    
    def __init__(self, expected_size=None):
        self.expected_size = expected_size
        self.bytes_written = 0
        self.using_disk = False
        self.memory_buffer = io.BytesIO()
        self.temp_file = None
        self.temp_path = None
        self.closed = False
        
        # If we know the size upfront and it's large, use disk immediately
        if expected_size and expected_size > MAX_MEMORY_SIZE:
            self._switch_to_disk()
    
    def _switch_to_disk(self):
        """Switch from memory to disk storage"""
        if not self.using_disk:
            print(f"📀 WEBDAV: Switching to disk storage after {self.bytes_written} bytes")
            fd, self.temp_path = tempfile.mkstemp()
            self.temp_file = os.fdopen(fd, 'w+b')
            
            # Write existing memory buffer to disk
            if self.memory_buffer.tell() > 0:
                self.memory_buffer.seek(0)
                self.temp_file.write(self.memory_buffer.read())
                self.memory_buffer = None
            
            self.using_disk = True
    
    def write(self, data):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        
        data_len = len(data)
        self.bytes_written += data_len
        
        # Check if we need to switch to disk
        if not self.using_disk and self.bytes_written > MAX_MEMORY_SIZE:
            self._switch_to_disk()
        
        # Write to appropriate storage
        if self.using_disk:
            return self.temp_file.write(data)
        else:
            return self.memory_buffer.write(data)
    
    def close(self):
        self.closed = True
        if self.temp_file:
            self.temp_file.close()
    
    def get_data(self):
        """Get all data as bytes (for small files)"""
        if self.using_disk:
            raise ValueError("File too large to read into memory")
        return self.memory_buffer.getvalue()
    
    def get_path(self):
        """Get temp file path (for large files)"""
        if not self.using_disk:
            # Need to flush to disk first
            self._switch_to_disk()
        self.temp_file.flush()
        return self.temp_path
    
    def cleanup(self):
        """Clean up temporary resources"""
        if self.temp_file and not self.temp_file.closed:
            self.temp_file.close()
        if self.temp_path and os.path.exists(self.temp_path):
            try:
                os.unlink(self.temp_path)
            except OSError:
                pass
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


class WebDAVAPIClient:
    """Isolated API client for WebDAV context"""
    
    def __init__(self):
        self._credentials = None
        self._api_client = None
        self._thread_local = threading.local()
        self._deleted_items = set()  # Track recently deleted items

    # services/webdav_provider.py (Fixed)

    def _get_isolated_session(self):
        """Get a thread-local API session"""
        if not hasattr(self._thread_local, 'api_client'):
            if VERBOSE_LOGGING:
                print("🔍 WEBDAV: Creating fresh API session for WebDAV thread")
            
            from services.auth import auth_service
            from utils.api import ApiClient
            
            # We refresh the tokens first.
            # The WebDAV thread is separate from the main
            # CLI, so its tokens will be stale.
            try:
                if VERBOSE_LOGGING:
                    print("🔍 WEBDAV: Forcing token refresh for new session...")
                auth_service.refresh_tokens()
                if VERBOSE_LOGGING:
                    print("✅ WEBDAV: Token refresh successful.")
            except Exception as e:
                # If refresh fails, we'll have to try with stale tokens
                if VERBOSE_LOGGING:
                    print(f"⚠️  WEBDAV: Token refresh failed ({e}), proceeding anyway...")

            # Now get the (hopefully) fresh details
            self._credentials = auth_service.get_auth_details()
            
            # Create a new "dumb" client
            fresh_api_client = ApiClient()
            
            # Inject the fresh tokens
            fresh_api_client.set_auth_tokens(
                self._credentials.get('token'), 
                self._credentials.get('newToken')
            )
            
            self._thread_local.api_client = fresh_api_client
            if VERBOSE_LOGGING:
                print("✅ WEBDAV: Fresh API session created")
            
        return self._thread_local.api_client
        
    def _get_isolated_session_without_token_refresh(self):
        """Get a thread-local API session"""
        if not hasattr(self._thread_local, 'api_client'):
            if VERBOSE_LOGGING:
                print("🔍 WEBDAV: Creating fresh API session for WebDAV thread")
            
            from services.auth import auth_service
            from utils.api import ApiClient
            
            self._credentials = auth_service.get_auth_details()
            
            fresh_api_client = ApiClient()
            fresh_api_client.set_auth_tokens(
                self._credentials.get('token'), 
                self._credentials.get('newToken')
            )
            
            self._thread_local.api_client = fresh_api_client
            if VERBOSE_LOGGING:
                print("✅ WEBDAV: Fresh API session created")
            
        return self._thread_local.api_client
    
    def get_folder_content(self, folder_uuid: str) -> Dict[str, Any]:
        """Get folder content using isolated API session"""
        try:
            if VERBOSE_LOGGING:
                print(f"🔍 WEBDAV: Getting content for folder {folder_uuid}")
            api_client = self._get_isolated_session()
            
            folders_response = api_client.get_folder_folders(folder_uuid, 0, 50)
            folders = folders_response.get('result', folders_response.get('folders', []))
            
            files_response = api_client.get_folder_files(folder_uuid, 0, 50)  
            files = files_response.get('result', files_response.get('files', []))
            
            if VERBOSE_LOGGING:
                print(f"✅ WEBDAV: Got {len(folders)} folders and {len(files)} files")
            return {'folders': folders, 'files': files}
            
        except Exception as e:
            if VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Error getting folder content: {e}")
            return {'folders': [], 'files': []}
    
    def get_credentials(self) -> Dict[str, Any]:
        """Get cached credentials"""
        if not self._credentials:
            from services.auth import auth_service
            self._credentials = auth_service.get_auth_details()
        return self._credentials
    
    def mark_deleted(self, path: str):
        """Mark a path as recently deleted"""
        self._deleted_items.add(path)
        # Clean up old entries after some time
        if len(self._deleted_items) > 100:
            self._deleted_items.clear()
    
    def is_recently_deleted(self, path: str) -> bool:
        """Check if a path was recently deleted"""
        return path in self._deleted_items


# Global instance for WebDAV
webdav_api = WebDAVAPIClient()


class InternxtDAVResource(DAVNonCollection):
    """Represents a file in Internxt Drive for WebDAV access"""
    
    def __init__(self, path: str, environ: dict, file_metadata: Optional[dict] = None, provider=None):
        super().__init__(path, environ)
        self.file_metadata = file_metadata or {}
        self.provider = provider
        self._upload_buffer: Optional['StreamingFileUpload'] = None
        
    def get_content_length(self) -> int:
        """Return file size"""
        return int(self.file_metadata.get('size', 0))
    
    def get_content_type(self) -> str:
        """Return MIME type based on file extension"""
        file_name = self.file_metadata.get('plainName', '')
        file_type = self.file_metadata.get('type', '')
        
        if file_type:
            full_name = f"{file_name}.{file_type}"
        else:
            full_name = file_name
            
        content_type, _ = mimetypes.guess_type(full_name)
        return content_type or 'application/octet-stream'
    
    def get_creation_date(self) -> float:
        """Return file creation time as timestamp"""
        try:
            created_at = self.file_metadata.get('createdAt', '')
            if created_at:
                from datetime import datetime
                dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                return dt.timestamp()
        except Exception:
            pass
        return time.time()
    
    def get_last_modified(self) -> float:
        """Return file modification time as timestamp"""
        try:
            # Priority: modificationTime > updatedAt (SDK-compatible fields)
            modification_time = self.file_metadata.get('modificationTime') or self.file_metadata.get('updatedAt')
            
            if modification_time:
                from datetime import datetime
                dt = datetime.fromisoformat(modification_time.replace('Z', '+00:00'))
                return dt.timestamp()
        except Exception:
            pass
        return time.time()
    
    def get_etag(self) -> str:
        """Return ETag for the file resource - FIXED: no extra quotes"""
        file_uuid = self.file_metadata.get('uuid', 'unknown')[:8]
        modified = int(self.get_last_modified())
        size = self.get_content_length()
        # Return WITHOUT quotes - WsgiDAV adds them automatically
        return f"{file_uuid}-{modified}-{size}"
    
    def support_etag(self) -> bool:
        """Support ETags for caching"""
        return True
    
    def support_ranges(self) -> bool:
        """Support byte ranges for resumable downloads"""
        return True
    
    def support_content_length(self) -> bool:
        """Support content length"""
        return True
    
    def support_modified(self) -> bool:
        """Support last modified date"""
        return True
    
    def get_content(self) -> Union[bytes, io.BytesIO]:
        """Return file content as a seekable file-like object"""
        try:
            from services.drive import drive_service
            from services.auth import auth_service

            # Pending uploads + missing uuids return an empty body without
            # touching the auth service (no creds needed for a 0-byte read).
            # This also makes get_content() callable from unit tests that
            # don't bother mocking auth_service.
            file_uuid = self.file_metadata.get('uuid')
            if not file_uuid or file_uuid.startswith('pending-'):
                return io.BytesIO(b"")

            credentials = auth_service.get_auth_details()
            user = credentials['user']
            mnemonic = user['mnemonic']
                
            print(f"📥 WEBDAV: Downloading file {file_uuid} for streaming...")
            
            api_client = webdav_api._get_isolated_session()
            network_auth = drive_service._get_network_auth(user)
            
            # Get download info
            metadata = api_client.get_file_metadata(file_uuid)
            bucket_id = metadata['bucket']
            network_file_id = metadata['fileId']
            file_size = int(metadata['size'])
            
            # Get download link
            links_response = api_client.get_download_links(bucket_id, network_file_id, auth=network_auth)
            download_url = links_response['shards'][0]['url']
            file_index_hex = links_response['index']
            
            # Download encrypted data
            encrypted_data = api_client.download_chunk(download_url)
            
            # Decrypt
            from services.crypto import crypto_service
            decrypted_data = crypto_service.decrypt_stream_internxt_protocol(
                encrypted_data, mnemonic, bucket_id, file_index_hex
            )
            
            # Trim to exact size if needed
            if len(decrypted_data) > file_size:
                decrypted_data = decrypted_data[:file_size]
            
            print(f"✅ WEBDAV: Downloaded {len(decrypted_data)} bytes, returning as BytesIO")
            
            return io.BytesIO(decrypted_data)
            
        except Exception as e:
            print(f"❌ WEBDAV: Error downloading file: {e}")
            import traceback
            traceback.print_exc()
            
            error_msg = f"Error downloading file: {str(e)}\n"
            return io.BytesIO(error_msg.encode('utf-8'))
    
    def begin_write(self, content_type=None):
        """Called when starting to write content to the file"""
        print(f"🔍 WEBDAV: begin_write() for {self.path}")
        
        # Get expected size from Content-Length header if available
        expected_size = None
        if hasattr(self, 'environ') and self.environ:
            content_length = self.environ.get('CONTENT_LENGTH', '')
            if content_length.isdigit():
                expected_size = int(content_length)
                print(f"📏 WEBDAV: Expected upload size: {expected_size} bytes")
        
        # Use hybrid upload handler
        self._upload_buffer = StreamingFileUpload(expected_size)
        return self._upload_buffer
    
    def end_write(self, with_errors: bool):
        """Called when writing is complete, with timestamp preservation support."""
        print(f"🏁 WEBDAV: end_write(with_errors={with_errors}) for {self.path}")

        if with_errors or self._upload_buffer is None:
            print("❌ WEBDAV: Upload had errors or no buffer, aborting")
            if self._upload_buffer is not None:
                self._upload_buffer.cleanup()
            return

        try:
            from services.drive import drive_service
            from services.auth import auth_service

            # Get parent folder UUID
            path_parts = self.path.strip('/').split('/')
            if len(path_parts) == 1:
                credentials = auth_service.get_auth_details()
                parent_uuid = credentials['user'].get('rootFolderId', '')
            else:
                parent_path = '/' + '/'.join(path_parts[:-1])
                resolved = drive_service.resolve_path(parent_path)
                parent_uuid = resolved['uuid']

            # Get file name and parse into name/extension
            file_name = path_parts[-1]
            plain_name, file_type = os.path.splitext(file_name)
            file_type = file_type.lstrip('.')

            # Check if we're updating an existing file or creating a new one
            is_update = (hasattr(self, 'file_metadata') and
                        self.file_metadata.get('uuid') and
                        not self.file_metadata.get('uuid', '').startswith('pending-'))

            # Prepare timestamps if preservation is enabled
            creation_time = None
            modification_time = None
            
            if hasattr(self, 'provider') and self.provider and hasattr(self.provider, 'preserve_timestamps') and self.provider.preserve_timestamps:
                try:
                    from datetime import datetime, timezone
                    from pathlib import Path
                    
                    # Try to extract timestamps from the uploaded file
                    if self._upload_buffer.using_disk:
                        # Large file - read timestamps from disk file
                        file_path = Path(self._upload_buffer.get_path())
                        stat_info = file_path.stat()
                        
                        mtime = datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc)
                        modification_time = mtime.isoformat()
                        
                        try:
                            # st_birthtime is macOS/BSD-only; the AttributeError
                            # branch covers Linux.  mypy's stat_result stub
                            # doesn't expose it, so silence it here.
                            ctime = datetime.fromtimestamp(stat_info.st_birthtime, tz=timezone.utc)  # type: ignore[attr-defined, unused-ignore]
                            creation_time = ctime.isoformat()
                        except AttributeError:
                            ctime = datetime.fromtimestamp(stat_info.st_ctime, tz=timezone.utc)
                            creation_time = ctime.isoformat()
                        
                        print(f"🕐 WEBDAV: Preserving timestamps (large file) - created: {creation_time}, modified: {modification_time}")
                    
                except Exception as e:
                    print(f"⚠️  WEBDAV: Could not extract timestamps: {e}")

            # Handle large files uploaded to disk
            if self._upload_buffer.using_disk:
                file_path = self._upload_buffer.get_path()
                print(f"📤 WEBDAV: Uploading large file ({self._upload_buffer.bytes_written} bytes) from disk")
                if is_update:
                    result = drive_service.update_file(self.file_metadata['uuid'], file_path)
                else:
                    result = drive_service.upload_file_to_folder(
                        file_path, 
                        parent_uuid, 
                        plain_name, 
                        file_type,
                        creation_time=creation_time,
                        modification_time=modification_time
                    )
            
            # Handle small files uploaded from memory
            else:
                file_data = self._upload_buffer.get_data()
                print(f"📤 WEBDAV: Uploading small file ({len(file_data)} bytes) from memory")
                
                # Create a temporary file, ensuring it's closed before use.
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file_type}' if file_type else '')
                tmp_file_path = tmp_file.name
                
                try:
                    # Write data and close the file to release the lock.
                    tmp_file.write(file_data)
                    tmp_file.close()
                    
                    # Extract timestamps from temp file if not already extracted and preservation is enabled
                    if hasattr(self, 'provider') and self.provider and hasattr(self.provider, 'preserve_timestamps') and self.provider.preserve_timestamps and not modification_time:
                        try:
                            from datetime import datetime, timezone
                            from pathlib import Path
                            
                            stat_info = Path(tmp_file_path).stat()
                            mtime = datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc)
                            modification_time = mtime.isoformat()
                            
                            try:
                                # st_birthtime is macOS/BSD-only; AttributeError covers Linux.
                                ctime = datetime.fromtimestamp(stat_info.st_birthtime, tz=timezone.utc)  # type: ignore[attr-defined, unused-ignore]
                                creation_time = ctime.isoformat()
                            except AttributeError:
                                ctime = datetime.fromtimestamp(stat_info.st_ctime, tz=timezone.utc)
                                creation_time = ctime.isoformat()
                            
                            print(f"🕐 WEBDAV: Preserving timestamps (small file) - created: {creation_time}, modified: {modification_time}")
                        except Exception as e:
                            print(f"⚠️  WEBDAV: Could not extract timestamps from temp file: {e}")

                    # Now that the file is closed, we can safely pass its path to upload
                    if is_update:
                        result = drive_service.update_file(self.file_metadata['uuid'], tmp_file_path)
                    else:
                        result = drive_service.upload_file_to_folder(
                            tmp_file_path, 
                            parent_uuid, 
                            plain_name, 
                            file_type,
                            creation_time=creation_time,
                            modification_time=modification_time
                        )
                finally:
                    # Clean up the temporary file after the upload operation is complete.
                    os.unlink(tmp_file_path)
            
            print("✅ WEBDAV: Upload successful!")

            # Update resource metadata and clear parent cache
            if isinstance(result, dict):
                self.file_metadata.update(result)
            if hasattr(self, 'parent') and hasattr(self.parent, '_content_cache'):
                self.parent._content_cache = None

        except Exception as e:
            print(f"❌ WEBDAV: Upload failed: {e}")
            import traceback
            traceback.print_exc()
            raise DAVError(HTTP_FORBIDDEN, f"Upload failed: {e}") from e
        finally:
            if self._upload_buffer is not None:
                self._upload_buffer.cleanup()
    
    def delete(self):
        """Delete this file"""
        print(f"🗑️ WEBDAV: Deleting file {self.path}")
        
        try:
            from services.drive import drive_service
            
            file_uuid = self.file_metadata.get('uuid')
            if not file_uuid or file_uuid.startswith('pending-'):
                raise DAVError(HTTP_NOT_FOUND, "File not found")
            
            # Use the actual trash_file method from drive_service
            drive_service.trash_file(file_uuid)

            # Mark as deleted to avoid unnecessary lookups
            webdav_api.mark_deleted(self.path)

            print(f"✅ WEBDAV: Deleted file {self.path}")

        except Exception as e:
            print(f"❌ WEBDAV: Error deleting file: {e}")
            raise DAVError(HTTP_FORBIDDEN, f"Could not delete file: {e}") from e
    
    def move_recursive(self, dest_path):
        """Move/rename this file"""
        print(f"📦 WEBDAV: Moving file from {self.path} to {dest_path}")
        
        try:
            from services.drive import drive_service
            
            file_uuid = self.file_metadata.get('uuid')
            if not file_uuid or file_uuid.startswith('pending-'):
                raise DAVError(HTTP_NOT_FOUND, "File not found")
            
            # Parse source and destination
            src_parts = self.path.strip('/').split('/')
            dest_parts = dest_path.strip('/').split('/')
            
            src_dir = '/' + '/'.join(src_parts[:-1]) if len(src_parts) > 1 else '/'
            dest_dir = '/' + '/'.join(dest_parts[:-1]) if len(dest_parts) > 1 else '/'
            
            new_name = dest_parts[-1]
            
            # Check if we need to move to different folder
            if src_dir != dest_dir:
                # Moving to different folder
                dest_parent = drive_service.resolve_path(dest_dir)
                drive_service.move_file(file_uuid, dest_parent['uuid'])

            # Check if we need to rename
            current_full_name = self.file_metadata.get('plainName', '')
            current_type = self.file_metadata.get('type', '')
            if current_type:
                current_full_name = f"{current_full_name}.{current_type}"

            if current_full_name != new_name:
                # Need to rename
                drive_service.rename_file(file_uuid, new_name)

            # Mark old path as deleted
            webdav_api.mark_deleted(self.path)

            print(f"✅ WEBDAV: Moved file to {dest_path}")

        except Exception as e:
            print(f"❌ WEBDAV: Error moving file: {e}")
            raise DAVError(HTTP_FORBIDDEN, f"Could not move file: {e}") from e
    
    def set_property(self, name, value, depth="0"):
        """Set a property on a file (called by PROPPATCH).

        Mirrors the folder implementation on InternxtDAVCollection so that
        rclone --metadata and file-manager clients can set timestamps on
        files, not just folders.
        """
        from services.drive import drive_service
        from wsgidav.util import parse_time_string
        from datetime import datetime, timezone

        print(f"🔍 WEBDAV: PROPPATCH set_property('{name}', '{value}') on file")

        if name in ("{DAV:}creationdate", "{urn:schemas-microsoft-com:}creationdate"):
            try:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                iso_timestamp = dt.isoformat()
                drive_service.set_file_timestamps(
                    self.file_metadata['uuid'],
                    creation_time=iso_timestamp
                )
            except Exception as e:
                print(f"❌ WEBDAV: Error setting file creationdate: {e}")
                raise DAVError(HTTP_FORBIDDEN, "Failed to set creation date") from e

        elif name in ("{DAV:}getlastmodified", "{urn:schemas-microsoft-com:}Win32LastModifiedTime"):
            try:
                ts = parse_time_string(value)
                if ts is None:
                    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    ts = dt.timestamp()

                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                iso_timestamp = dt.isoformat()

                drive_service.set_file_timestamps(
                    self.file_metadata['uuid'],
                    modification_time=iso_timestamp
                )
            except Exception as e:
                print(f"❌ WEBDAV: Error setting file getlastmodified: {e}")
                raise DAVError(HTTP_FORBIDDEN, "Failed to set modification date") from e

        else:
            print(f"⚠️  WEBDAV: Ignored file set_property for '{name}'")
            super().set_property(name, value, depth)

    def copy_move(self, dest_path):
        """Copy this file"""
        print(f"📋 WEBDAV: Copying file from {self.path} to {dest_path}")
        
        try:
            from services.drive import drive_service
            from services.auth import auth_service
            
            # Parse destination
            dest_parts = dest_path.strip('/').split('/')
            
            if len(dest_parts) > 1:
                parent_path = '/' + '/'.join(dest_parts[:-1])
                resolved = drive_service.resolve_path(parent_path)
                parent_uuid = resolved['uuid']
            else:
                credentials = auth_service.get_auth_details()
                parent_uuid = credentials['user'].get('rootFolderId', '')
            
            # Use the copy_item method from drive_service
            drive_service.copy_item(self.file_metadata['uuid'], parent_uuid)

            print(f"✅ WEBDAV: Copied file to {dest_path}")

        except Exception as e:
            print(f"❌ WEBDAV: Error copying file: {e}")
            raise DAVError(HTTP_FORBIDDEN, f"Could not copy file: {e}") from e


class InternxtDAVCollection(DAVCollection):
    """Represents a folder in Internxt Drive for WebDAV access"""
    
    def __init__(self, path: str, environ: dict, folder_metadata: Optional[dict] = None, provider=None):
        super().__init__(path, environ)
        self.folder_metadata = folder_metadata or {}
        self.provider = provider
        self._content_cache: Optional[Dict[str, Any]] = None
        self._content_cached_time: float = 0.0
        self.CACHE_TIMEOUT = 300 # 5 min
        
    def get_member_names(self) -> List[str]:
        """Return list of files and folders in this directory"""
        try:
            content = self._get_content()
            names = content.get('names', [])
            if VERBOSE_LOGGING:
                print(f"✅ WEBDAV: Returning {len(names)} member names: {names}")
            return names
        except Exception as e:
            if VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Error getting member names for {self.path}: {e}")
            return []
    
    def get_member(self, name: str) -> Optional[Union[DAVCollection, DAVNonCollection]]:
        """Get a specific member (file or folder) by name"""
        try:
            if VERBOSE_LOGGING:
                print(f"🔍 WEBDAV: get_member({name}) for path: {self.path}")
            content = self._get_content()
            
            # Check if it's a folder
            for folder in content['folders']:
                folder_name = folder.get('plainName', folder.get('name', ''))
                if folder_name == name:
                    child_path = f"{self.path.rstrip('/')}/{name}" if self.path != '/' else f"/{name}"
                    if VERBOSE_LOGGING:
                        print(f"✅ WEBDAV: Found folder: {name}")
                    return InternxtDAVCollection(child_path, self.environ, folder, provider=self.provider)
            
            # Check if it's a file
            for file in content['files']:
                file_name = file.get('plainName', '')
                file_type = file.get('type', '')
                display_name = f"{file_name}.{file_type}" if file_type else file_name
                
                if display_name == name:
                    child_path = f"{self.path.rstrip('/')}/{name}" if self.path != '/' else f"/{name}"
                    if VERBOSE_LOGGING:
                        print(f"✅ WEBDAV: Found file: {name}")
                    return InternxtDAVResource(child_path, self.environ, file, provider=self.provider)
            
            # Only log if not recently deleted
            child_path = f"{self.path.rstrip('/')}/{name}" if self.path != '/' else f"/{name}"
            if not webdav_api.is_recently_deleted(child_path) and VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Member '{name}' not found in {self.path}")
            return None
            
        except Exception as e:
            if VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Error getting member {name}: {e}")
                import traceback
                traceback.print_exc()
            return None
    
    def create_empty_resource(self, name: str):
        """Create a new empty file resource"""
        print(f"🔍 WEBDAV: create_empty_resource({name}) in {self.path}")
        
        # Create a placeholder file resource
        file_metadata = {
            'plainName': name.rsplit('.', 1)[0] if '.' in name else name,
            'type': name.rsplit('.', 1)[1] if '.' in name else '',
            'size': 0,
            'uuid': f'pending-{name}',
            'isUploading': True
        }
        
        child_path = f"{self.path.rstrip('/')}/{name}" if self.path != '/' else f"/{name}"
        return InternxtDAVResource(child_path, self.environ, file_metadata, provider=self.provider)
    
    def create_collection(self, name: str):
        """Create a new folder"""
        print(f"🔍 WEBDAV: create_collection({name}) in {self.path}")
        
        try:
            from services.drive import drive_service
            from services.auth import auth_service
            
            # Get parent folder UUID
            if self.path == '/' or self.path == '':
                credentials = auth_service.get_auth_details()
                parent_uuid = credentials['user'].get('rootFolderId', '')
            else:
                resolved = drive_service.resolve_path(self.path)
                parent_uuid = resolved['uuid']
            
            # Create folder using drive_service
            new_folder = drive_service.create_folder(name, parent_uuid)
            
            # Clear cache
            self._content_cache = None
            
            print(f"✅ WEBDAV: Created folder {name} with UUID {new_folder['uuid']}")
            
            child_path = f"{self.path.rstrip('/')}/{name}" if self.path != '/' else f"/{name}"
            return InternxtDAVCollection(child_path, self.environ, new_folder, provider=self.provider)
            
        except Exception as e:
            print(f"❌ WEBDAV: Error creating folder: {e}")
            raise DAVError(HTTP_FORBIDDEN, f"Could not create folder: {e}")
        
    def set_property(self, name, value, depth="0"):
        """Set a property (called by PROPPATCH)."""
        from services.drive import drive_service
        from wsgidav.util import parse_time_string
        from datetime import datetime, timezone

        print(f"🔍 WEBDAV: PROPPATCH set_property('{name}', '{value}')")

        if name in ("{DAV:}creationdate", "{urn:schemas-microsoft-com:}creationdate"):
            # Not all clients support setting this, but we try
            try:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                iso_timestamp = dt.isoformat()
                drive_service.set_folder_timestamps(
                    self.folder_metadata['uuid'],
                    creation_time=iso_timestamp
                )
            except Exception as e:
                print(f"❌ WEBDAV: Error setting creationdate: {e}")
                raise DAVError(HTTP_FORBIDDEN, "Failed to set creation date") from e

        elif name in ("{DAV:}getlastmodified", "{urn:schemas-microsoft-com:}Win32LastModifiedTime"):
            try:
                # Accept RFC 1123 ("Wed, 21 Oct 2015 07:28:00 GMT") via wsgidav
                # helper, falling back to ISO 8601 / RFC 3339 via stdlib.
                ts = parse_time_string(value)
                if ts is None:
                    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    ts = dt.timestamp()

                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                iso_timestamp = dt.isoformat()

                drive_service.set_folder_timestamps(
                    self.folder_metadata['uuid'],
                    modification_time=iso_timestamp
                )
            except Exception as e:
                print(f"❌ WEBDAV: Error setting getlastmodified: {e}")
                raise DAVError(HTTP_FORBIDDEN, "Failed to set modification date") from e

        else:
            print(f"⚠️  WEBDAV: Ignored set_property for '{name}'")
            super().set_property(name, value, depth)
    
    def delete(self):
        """Delete this folder"""
        print(f"🗑️ WEBDAV: Deleting folder {self.path}")
        
        if self.path == '/' or self.path == '':
            raise DAVError(HTTP_FORBIDDEN, "Cannot delete root folder")
        
        try:
            from services.drive import drive_service
            
            folder_uuid = self.folder_metadata.get('uuid')
            if not folder_uuid:
                resolved = drive_service.resolve_path(self.path)
                folder_uuid = resolved['uuid']
            
            # Use the actual trash_folder method from drive_service
            drive_service.trash_folder(folder_uuid)

            # Mark as deleted to avoid unnecessary lookups
            webdav_api.mark_deleted(self.path)

            print(f"✅ WEBDAV: Deleted folder {self.path}")

        except Exception as e:
            print(f"❌ WEBDAV: Error deleting folder: {e}")
            raise DAVError(HTTP_FORBIDDEN, f"Could not delete folder: {e}") from e
    
    def move_recursive(self, dest_path):
        """Move/rename this folder"""
        print(f"📦 WEBDAV: Moving folder from {self.path} to {dest_path}")
        
        try:
            from services.drive import drive_service
            
            folder_uuid = self.folder_metadata.get('uuid')
            if not folder_uuid:
                resolved = drive_service.resolve_path(self.path)
                folder_uuid = resolved['uuid']
            
            # Parse source and destination
            src_parts = self.path.strip('/').split('/')
            dest_parts = dest_path.strip('/').split('/')
            
            src_dir = '/' + '/'.join(src_parts[:-1]) if len(src_parts) > 1 else '/'
            dest_dir = '/' + '/'.join(dest_parts[:-1]) if len(dest_parts) > 1 else '/'
            
            new_name = dest_parts[-1]
            
            # Check if we need to move to different folder
            if src_dir != dest_dir:
                # Moving to different folder
                dest_parent = drive_service.resolve_path(dest_dir)
                drive_service.move_folder(folder_uuid, dest_parent['uuid'])

            # Check if we need to rename
            current_name = self.folder_metadata.get('plainName', self.folder_metadata.get('name', ''))

            if current_name != new_name:
                # Need to rename
                drive_service.rename_folder(folder_uuid, new_name)
            
            # Mark old path as deleted
            webdav_api.mark_deleted(self.path)
            
            print(f"✅ WEBDAV: Moved folder to {dest_path}")
            
        except Exception as e:
            print(f"❌ WEBDAV: Error moving folder: {e}")
            raise DAVError(HTTP_FORBIDDEN, f"Could not move folder: {e}")
    
    def copy_recursive(self, dest_path):
        """Copy this folder recursively via the drive service."""
        print(f"📋 WEBDAV: Copying folder from {self.path} to {dest_path}")

        try:
            from services.drive import drive_service
            from services.auth import auth_service

            # Parse destination
            dest_parts = dest_path.strip('/').split('/')
            if len(dest_parts) > 1:
                parent_path = '/' + '/'.join(dest_parts[:-1])
                resolved = drive_service.resolve_path(parent_path)
                parent_uuid = resolved['uuid']
            else:
                credentials = auth_service.get_auth_details()
                parent_uuid = credentials['user'].get('rootFolderId', '')

            dest_name = dest_parts[-1] if dest_parts else None
            result = drive_service.copy_folder(
                self.folder_metadata['uuid'], parent_uuid, dest_name
            )
            print(f"✅ WEBDAV: Copied folder to {dest_path} ({result['message']})")

        except Exception as e:
            print(f"❌ WEBDAV: Error copying folder: {e}")
            raise DAVError(HTTP_FORBIDDEN, f"Could not copy folder: {e}") from e
    
    def get_creation_date(self) -> float:
        """Return file creation time as timestamp"""
        try:
            # Priority: creationTime > createdAt (SDK-compatible fields)
            creation_time = self.folder_metadata.get('creationTime') or self.folder_metadata.get('createdAt')
            
            if creation_time:
                from datetime import datetime
                dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                return dt.timestamp()
        except Exception:
            pass
        return super().get_creation_date() # Fallback
    
    def get_last_modified(self) -> float:
        """Return folder modification time.

        WsgiDAV's base get_last_modified() returns None for the root
        collection unless overridden, but our annotation promises -> float.
        Always return a real float so callers (notably get_etag) can int()
        it without crashing on PROPFIND of '/'.
        """
        try:
            updated_at = self.folder_metadata.get('updatedAt', '')
            if updated_at:
                from datetime import datetime
                dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                return dt.timestamp()
        except Exception:
            pass
        try:
            base = super().get_last_modified()
            if base is not None:
                return float(base)
        except Exception:
            pass
        # Final fallback: server start time (server-uptime-stable, not
        # tied to a non-existent folder timestamp).
        return 0.0

    def get_etag(self) -> str:
        """Return ETag for the folder resource - FIXED: no extra quotes"""
        folder_uuid = self.folder_metadata.get('uuid', 'root')
        if folder_uuid != 'root':
            folder_uuid = folder_uuid[:8]
        try:
            modified = int(self.get_last_modified() or 0)
        except (TypeError, ValueError):
            modified = 0

        try:
            member_count = len(self._get_content().get('names', []))
        except Exception:
            member_count = 0

        # Return WITHOUT quotes - WsgiDAV adds them automatically
        return f"{folder_uuid}-{modified}-{member_count}"
    
    def support_etag(self) -> bool:
        """Support ETags for caching"""
        return True
    
    def support_ranges(self) -> bool:
        """Collections don't support ranges"""
        return False
    
    def support_content_length(self) -> bool:
        """Collections don't have content length"""
        return False
    
    def support_modified(self) -> bool:
        """Support last modified date"""
        return True
    
    def _get_content(self) -> Dict[str, Any]:
        """Get folder content with caching"""
        current_time = time.time()
        
        # Check cache
        if (self._content_cache is not None and 
            current_time - self._content_cached_time < self.CACHE_TIMEOUT):
            return self._content_cache
        
        try:
            if VERBOSE_LOGGING:
                print(f"🔍 WEBDAV: Fetching fresh content for: {self.path}")
            
            if self.path == '/' or self.path == '':
                # Root folder
                credentials = webdav_api.get_credentials()
                root_folder_uuid = credentials['user'].get('rootFolderId', '')
                
                if not root_folder_uuid:
                    print("❌ WEBDAV: No root folder UUID found")
                    return {'folders': [], 'files': [], 'names': []}
                
                content = webdav_api.get_folder_content(root_folder_uuid)
                
            else:
                # Non-root folder - use drive_service
                from services.drive import drive_service
                content = drive_service.list_folder_with_paths(self.path)
            
            # Build names list
            names = []
            for folder in content.get('folders', []):
                folder_name = folder.get('plainName', folder.get('name', ''))
                if folder_name:
                    names.append(folder_name)
            
            for file in content.get('files', []):
                file_name = file.get('plainName', '')
                file_type = file.get('type', '')
                display_name = f"{file_name}.{file_type}" if file_type else file_name
                if display_name:
                    names.append(display_name)
            
            result = {
                'folders': content.get('folders', []),
                'files': content.get('files', []),
                'names': names
            }
            
            # Cache the result
            self._content_cache = result
            self._content_cached_time = current_time
            
            if VERBOSE_LOGGING:
                print(f"✅ WEBDAV: Cached content - {len(names)} total items")
            return result
            
        except Exception as e:
            if VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Error getting content for {self.path}: {e}")
                import traceback
                traceback.print_exc()
            return {'folders': [], 'files': [], 'names': []}


class InternxtDAVProvider(DAVProvider):
    """WebDAV Provider for Internxt Drive"""
    
    def __init__(self, preserve_timestamps: bool = True):
        super().__init__()
        self.preserve_timestamps = preserve_timestamps
        print(f"🕐 InternxtDAVProvider initialized with timestamp preservation: {preserve_timestamps}")
        self.readonly = False
        # WebDAV uploads go through per-request temp spool files whose random
        # paths can never match a resume checkpoint on a retry — skip writing
        # checkpoints (the in-session part repair pass still runs).
        from services.drive import drive_service
        drive_service.resume_uploads = False
        
        print("🔍 WEBDAV: Initializing InternxtDAVProvider...")
        
        try:
            credentials = webdav_api.get_credentials()
            user_email = credentials['user'].get('email', 'unknown')
            print(f"✅ WEBDAV: Provider initialized with credentials for: {user_email}")
        except Exception as e:
            print(f"❌ WEBDAV: Provider initialization failed: {e}")
            raise
    
    def get_resource_inst(self, path: str, environ: dict) -> Optional[Union[DAVCollection, DAVNonCollection]]:
        """Get DAV resource for given path"""
        try:
            if not path.startswith('/'):
                path = '/' + path
            
            if VERBOSE_LOGGING:
                print(f"🔍 WEBDAV: get_resource_inst() for path: {path}")
            
            # Root is always a collection
            if path == '/' or path == '':
                # *** FIX: Pass 'provider=self' ***
                return InternxtDAVCollection(path=path, environ=environ, provider=self)
            
            # Skip if recently deleted
            if webdav_api.is_recently_deleted(path):
                if VERBOSE_LOGGING:
                    print(f"ℹ️  WEBDAV: Skipping recently deleted path: {path}")
                return None
            
            # For other paths, check if it's a file or folder
            path_parts = path.strip('/').split('/')
            parent_path = '/' + '/'.join(path_parts[:-1]) if len(path_parts) > 1 else '/'
            item_name = path_parts[-1]
            
            # Get parent collection
            parent_collection = InternxtDAVCollection(path=parent_path, environ=environ, provider=self)
            
            # Try to get the specific member
            # (The get_member method itself also needs to be fixed
            # to pass the provider to its children, but this
            # change ensures 'parent_collection' has the provider)
            member = parent_collection.get_member(item_name)
            if member:
                return member
            
            # If not found, return None (404)
            if not webdav_api.is_recently_deleted(path) and VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Resource not found: {path}")
            return None
                
        except Exception as e:
            if VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Error in get_resource_inst({path}): {e}")
                import traceback
                traceback.print_exc()
            return None
    
    def exists(self, path: str, environ: dict) -> bool:
        """Check if path exists"""
        try:
            resource = self.get_resource_inst(path, environ)
            return resource is not None
        except Exception as e:
            if VERBOSE_LOGGING:
                print(f"❌ WEBDAV: Error in exists({path}): {e}")
            return False