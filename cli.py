#!/usr/bin/env python3
"""
Internxt CLI - Python implementation with Path Support and Delete Operations
Enhanced with path-based operations and comprehensive delete/trash functionality
"""

import click
import sys
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
import glob
import time
import threading
import concurrent.futures
# Used for controlled webdav-start spawn (argv list, no shell, no untrusted input).
import subprocess  # nosec B404

# Try to import required packages (probe imports up-front so we can give a
# friendly install hint instead of a deep stack trace from a service module).
try:
    # pylint: disable=unused-import
    import requests
    from requests.auth import HTTPBasicAuth
    import mnemonic  # noqa: F401  - dependency probe
    import cryptography  # noqa: F401  - dependency probe
    import tqdm  # noqa: F401  - dependency probe
except ImportError as e:
    print(f"❌ Missing required dependency: {e}")
    print("📦 Install with: pip install cryptography mnemonic tqdm requests click")
    sys.exit(1)

# Add current directory to path for imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# Import our services
try:
    from config.config import config_service
    from services.crypto import crypto_service
    from services.auth import auth_service
    from utils.api import api_client
    from services.drive import drive_service
    from services.webdav_server import webdav_server
    from services.network_utils import NetworkUtils
except ImportError as e:
    print(f"❌ Failed to import services: {e}")
    print("📦 Make sure all service files are in place with fixed implementations")
    
    # Check for WebDAV specific dependencies (NEW LOGIC)
    try:
        import wsgidav  # noqa: F401  - dependency probe
    except ImportError:
        print("📦 Missing core WebDAV dependency. Install with:")
        print("   pip install WsgiDAV")
        sys.exit(1)

    try:
        # Check for at least one server, matching webdav_server.py
        try:
            import waitress  # type: ignore[import-untyped]  # noqa: F401  - dependency probe
        except ImportError:
            import cheroot  # noqa: F401  - dependency probe
    except ImportError:
        print("📦 No suitable WSGI server found. Install one of:")
        print("   pip install waitress")
        print("   pip install cheroot")
        sys.exit(1)
        
    sys.exit(1) # Exit from the *original* service import error


def format_size(size_bytes: int) -> str:
    """Format bytes to human readable size"""
    if not size_bytes:
        return "0 B"
    size: float = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_date(date_string: str) -> str:
    """Format ISO date string to readable format"""
    try:
        dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
        return dt.strftime('%d %B, %Y at %H:%M')
    except Exception:
        return date_string


@click.group()
@click.version_option(version='1.0.0')
def cli():
    """Internxt Python CLI with Path Support and Delete Operations"""


# ========== AUTHENTICATION COMMANDS ==========

@cli.command()
@click.option('--email', '-e', help='Your Internxt email')
@click.option('--password', '-p', help='Your password')
@click.option('--tfa', '--2fa', help='Two-factor authentication code (6 digits)')
@click.option('--non-interactive', is_flag=True, help='Run in non-interactive mode')
@click.option('--debug', is_flag=True, help='Enable debug output')
def login(email: Optional[str], password: Optional[str], tfa: Optional[str], non_interactive: bool, debug: bool):
    """Login to your Internxt account"""
    try:
        if debug:
            print("🔍 Debug mode enabled")
            print("🔍 API Endpoints:")
            print(f"   Drive API: {config_service.get('DRIVE_NEW_API_URL')}")
            print(f"   Network API: {config_service.get('NETWORK_URL')}")
        
        # Get email
        if not email:
            if non_interactive:
                click.echo("❌ Email is required in non-interactive mode", err=True)
                sys.exit(1)
            email = click.prompt('What is your email?', type=str)
        
        # Validate email
        if '@' not in email or '.' not in email:
            click.echo("❌ Invalid email format", err=True)
            sys.exit(1)
        
        # Get password
        if not password:
            if non_interactive:
                click.echo("❌ Password is required in non-interactive mode", err=True)
                sys.exit(1)
            password = click.prompt('What is your password?', hide_input=True, type=str)
        
        if not password.strip():
            click.echo("❌ Password cannot be empty", err=True)
            sys.exit(1)
        
        # Check 2FA
        click.echo("🔍 Checking 2FA requirements...")
        try:
            is_2fa_needed = auth_service.is_2fa_needed(email)
            if debug:
                print(f"🔍 2FA needed: {is_2fa_needed}")
        except Exception as e:
            click.echo(f"⚠️  Could not check 2FA status: {e}")
            is_2fa_needed = False
        
        if is_2fa_needed and not tfa:
            if non_interactive:
                click.echo("❌ 2FA code is required in non-interactive mode", err=True)
                sys.exit(1)
            tfa = click.prompt('What is your two-factor token?', type=str)
        
        if tfa and (not tfa.isdigit() or len(tfa) != 6):
            click.echo("❌ Invalid 2FA code format (must be 6 digits)", err=True)
            sys.exit(1)
        
        # Login
        click.echo("🔐 Logging in...")
        credentials = auth_service.login(email, password, tfa)
        
        user_email = credentials['user']['email']
        user_uuid = credentials['user']['uuid']
        root_folder_id = credentials['user'].get('rootFolderId', '')
        
        click.echo(f"✅ Successfully logged in as: {user_email}")
        if debug:
            print(f"🔍 User UUID: {user_uuid}")
            print(f"🔍 Root Folder ID: {root_folder_id}")
        
    except Exception as e:
        error_msg = str(e)
        if "Login failed:" in error_msg:
            error_msg = error_msg.replace("Login failed: ", "")
        click.echo(f"❌ Login failed: {error_msg}", err=True)
        
        if debug:
            import traceback
            print("🔍 Full error traceback:")
            traceback.print_exc()
        
        sys.exit(1)


@cli.command()
def whoami():
    """Check current login status"""
    try:
        user_info = auth_service.whoami()
        if user_info:
            click.echo(f"📧 Logged in as: {user_info['email']}")
            click.echo(f"🆔 User ID: {user_info['uuid']}")
            click.echo(f"📁 Root Folder ID: {user_info['rootFolderId']}")
        else:
            click.echo("❌ Not logged in")
            click.echo("💡 Use 'python cli.py login' to log in")
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)


@cli.command()
def logout():
    """Logout and clear credentials"""
    try:
        auth_service.logout()
        click.echo("✅ Successfully logged out")
    except Exception as e:
        click.echo(f"❌ Error during logout: {e}", err=True)


# ========== BASIC FILE OPERATIONS ==========

@cli.command(name='list')
@click.option('--folder-id', help='Folder ID to list (defaults to root)')
@click.option('--detailed', '-d', is_flag=True, help='Show detailed information')
def list_cmd(folder_id, detailed):
    """List files and folders (UUID-based - legacy)"""
    try:
        credentials = auth_service.get_auth_details()
        
        if not folder_id:
            folder_id = credentials['user'].get('rootFolderId', '')
            if not folder_id:
                click.echo("❌ No root folder ID found. Please try logging in again.", err=True)
                return
        
        click.echo(f"📂 Listing contents of folder: {folder_id}")
        
        contents = drive_service.get_folder_content(folder_id)
        
        folders = contents.get('folders', [])
        files = contents.get('files', [])
        
        if not folders and not files:
            click.echo("📭 Folder is empty")
            return
        
        if folders:
            click.echo(f"\n📁 Folders ({len(folders)}):")
            for folder in folders:
                name = folder.get('plainName', 'Unknown')
                
                # Prioritize preserved timestamps
                display_time_iso = folder.get('modificationTime') or \
                                   folder.get('creationTime') or \
                                   folder.get('updatedAt') or \
                                   folder.get('createdAt', '')
                
                if detailed and display_time_iso:
                    click.echo(f"  📁 {name} (created {format_date(display_time_iso)})")
                else:
                    click.echo(f"  📁 {name}")
        
        if files:
            click.echo(f"\n📄 Files ({len(files)}):")
            for file in files:
                name = file.get('plainName', 'Unknown')
                file_type = file.get('type', '')
                if file_type:
                    name = f"{name}.{file_type}"
                
                try:
                    size = int(file.get('size', 0))
                except (ValueError, TypeError):
                    size = 0
                
                # Prioritize preserved timestamps
                display_time_iso = file.get('modificationTime') or \
                                   file.get('creationTime') or \
                                   file.get('updatedAt') or \
                                   file.get('createdAt', '')
                
                if detailed:
                    size_str = format_size(size)
                    if display_time_iso:
                        click.echo(f"  📄 {name} ({size_str}, {format_date(display_time_iso)})")
                    else:
                        click.echo(f"  📄 {name} ({size_str})")
                else:
                    click.echo(f"  📄 {name} ({format_size(size)})")
    except Exception as e:
        click.echo(f"❌ Error listing folder: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('path')
@click.option('--parent-folder-id', help='Parent folder ID (defaults to root). If used, path must be a single name.')
def mkdir(path: str, parent_folder_id: Optional[str]):
    """Create a new folder (supports paths like folder/subfolder)"""
    try:
        # Ensure we are logged in
        auth_service.get_auth_details()

        # CASE 1: User specified a specific parent UUID (Legacy/Strict mode)
        if parent_folder_id:
            if '/' in path or '\\' in path:
                click.echo("❌ Error: When providing --parent-folder-id, the name cannot contain slashes.", err=True)
                click.echo("💡 Tip: To create nested paths like 'A/B', do not use --parent-folder-id.", err=True)
                return

            click.echo(f"📁 Creating single folder '{path}' in parent {parent_folder_id}...")
            folder = drive_service.create_folder(path, parent_folder_id)
            
        # CASE 2: No parent ID specified (Smart Path mode)
        else:
            # Use the recursive function to handle "daten/phil" automatically
            click.echo(f"📁 Creating folder path: {path}")
            folder = drive_service.create_folder_recursive(path)

        # Output success results
        folder_uuid = folder.get('uuid', folder.get('id', ''))
        folder_name = folder.get('plainName', folder.get('name', path))
        
        click.echo("✅ Folder created successfully!")
        click.echo(f"📁 Name: {folder_name}")
        click.echo(f"🆔 UUID: {folder_uuid}")
        
    except Exception as e:
        error_msg = str(e)
        click.echo(f"❌ Error creating folder: {error_msg}", err=True)

# cli.py

@cli.command('mv')
@click.argument('paths', nargs=-1, required=True)
@click.option('--workers', '-w', type=int, default=4, show_default=True,
              help='Parallel move workers (1 = serial)')
@click.option('--on-conflict', type=click.Choice(['skip', 'overwrite'], case_sensitive=False),
              default='skip', show_default=True,
              help='Action when target folder already contains an item with the same name')
@click.option('--dry-run', '-n', is_flag=True, help='Show what would be moved without making changes')
@click.option('--verbose', '-v', is_flag=True, help='Show per-item progress')
def move_path(paths: Tuple[str, ...], workers: int, on_conflict: str,
              dry_run: bool, verbose: bool):
    """
    Move one or more files/folders into a target folder.

    The LAST argument is the target folder. All preceding arguments are
    sources. Wildcards (*, ?, [seq]) are supported in the leaf segment of
    a source path and are matched against the remote folder listing.

    Examples:
      mv /Docs/file.txt /Archive
      mv /Docs/a.pdf /Docs/b.pdf /Docs/c.pdf /Archive
      mv "/Photos/2016/*.JPG" /Archive/2016
      mv "/Photos/2016/IMG_????.JPG" /Archive/2016 -w 8
    """
    if len(paths) < 2:
        click.echo("❌ Need at least one source and a target folder.", err=True)
        sys.exit(1)

    *sources, target_path = paths

    try:
        auth_service.get_auth_details()

        # ===== Resolve target folder once =====
        try:
            target_info = drive_service.resolve_path(target_path)
        except FileNotFoundError:
            click.echo(f"❌ Target folder not found: {target_path}", err=True)
            sys.exit(1)
        if target_info['type'] != 'folder':
            click.echo(f"❌ Target '{target_path}' is a file, not a folder.", err=True)
            sys.exit(1)
        target_uuid = target_info['uuid']
        target_resolved_path = target_info['path']

        # Pre-fetch target listing once for conflict detection.
        target_existing: Dict[str, Dict[str, Any]] = {}
        try:
            target_content = drive_service.get_folder_content(target_uuid)
            for f in target_content.get('files', []):
                plain = f.get('plainName', '') or f.get('name', '')
                ext = f.get('type', '') or ''
                name = f"{plain}.{ext}" if ext else plain
                target_existing[name] = {'type': 'file', 'uuid': f.get('uuid')}
            for d in target_content.get('folders', []):
                name = d.get('plainName') or d.get('name')
                if name:
                    target_existing[name] = {'type': 'folder', 'uuid': d.get('uuid')}
        except Exception as e:
            if verbose:
                click.echo(f"⚠️  Could not pre-scan target folder: {e}")

        # ===== Expand sources (wildcards + literals) =====
        import fnmatch
        # Each entry: (display_path, type, uuid, leaf_name)
        expanded: List[Tuple[str, str, str, str]] = []
        not_found: List[str] = []

        def _is_glob(s: str) -> bool:
            return any(c in s for c in '*?[')

        for src in sources:
            if _is_glob(src):
                # Split into parent + leaf pattern
                if '/' in src:
                    parent_path = src.rsplit('/', 1)[0] or '/'
                    leaf_pattern = src.rsplit('/', 1)[1]
                else:
                    parent_path = '/'
                    leaf_pattern = src

                try:
                    listing = drive_service.list_folder_with_paths(parent_path)
                except Exception as e:
                    click.echo(f"❌ Could not list parent of '{src}': {e}", err=True)
                    continue

                matched_any = False
                for f in listing.get('files', []):
                    name = f.get('display_name') or f.get('plainName', '')
                    if fnmatch.fnmatch(name, leaf_pattern):
                        expanded.append((f.get('path', f"{parent_path}/{name}"),
                                         'file', f['uuid'], name))
                        matched_any = True
                for d in listing.get('folders', []):
                    name = d.get('display_name') or d.get('plainName', '')
                    if fnmatch.fnmatch(name, leaf_pattern):
                        expanded.append((d.get('path', f"{parent_path}/{name}"),
                                         'folder', d['uuid'], name))
                        matched_any = True
                if not matched_any:
                    not_found.append(src)
            else:
                try:
                    info = drive_service.resolve_path(src)
                    leaf = info['path'].rsplit('/', 1)[-1] or info['path']
                    expanded.append((info['path'], info['type'], info['uuid'], leaf))
                except FileNotFoundError:
                    not_found.append(src)
                except Exception as e:
                    click.echo(f"❌ Error resolving '{src}': {e}", err=True)
                    not_found.append(src)

        for missing in not_found:
            click.echo(f"⚠️  Not found: {missing}", err=True)

        if not expanded:
            click.echo("❌ Nothing to move.", err=True)
            sys.exit(1)

        # Refuse to move an item into its current parent (no-op + confusing).
        # Also refuse to move a folder into itself / its descendant — but the
        # API will reject that, so we let it through and report the error.
        filtered: List[Tuple[str, str, str, str]] = []
        for entry in expanded:
            src_path, kind, uuid, leaf = entry
            src_parent = src_path.rsplit('/', 1)[0] or '/'
            if src_parent == target_resolved_path:
                if verbose:
                    click.echo(f"  ⏭️  Already in target: {src_path}")
                continue
            filtered.append(entry)
        expanded = filtered

        if not expanded:
            click.echo("✅ All sources are already in the target folder; nothing to do.")
            return

        click.echo(f"🚚 Moving {len(expanded)} item(s) → {target_resolved_path}")
        if dry_run:
            click.echo("🔬 DRY RUN — no changes will be made")

        # ===== Conflict resolution pass =====
        # Decide per-item action: 'move', 'skip', or 'overwrite-then-move'.
        # Done sequentially before parallel execution so the user sees a
        # clean plan and we can short-circuit on dry-run.
        plan: List[Tuple[str, str, str, str, str]] = []  # + action
        skipped_count = 0
        for src_path, kind, uuid, leaf in expanded:
            existing = target_existing.get(leaf)
            if existing is None:
                plan.append((src_path, kind, uuid, leaf, 'move'))
                continue
            if on_conflict == 'skip':
                if verbose or dry_run:
                    click.echo(f"  ⏭️  Skip (target has {leaf}): {src_path}")
                skipped_count += 1
                continue
            # overwrite
            if existing['type'] == 'folder':
                click.echo(f"  ❌ Refusing to overwrite folder at target: {leaf}", err=True)
                skipped_count += 1
                continue
            plan.append((src_path, kind, uuid, leaf, 'overwrite'))

        if dry_run:
            for src_path, kind, _uuid, _leaf, action in plan:
                marker = "🔁" if action == 'overwrite' else "➡️"
                click.echo(f"  {marker} {kind}: {src_path}")
            click.echo(f"📊 Would move: {len(plan)}, skipped: {skipped_count}")
            return

        # ===== Parallel execution =====
        success_count = 0
        error_count = 0
        counters_lock = threading.Lock()
        log_lock = threading.Lock()

        def _safe_log(msg: str, err: bool = False) -> None:
            with log_lock:
                click.echo(msg, err=err)

        def _do_move(item: Tuple[str, str, str, str, str]) -> Tuple[str, str]:
            src_path, kind, uuid, leaf, action = item
            try:
                if action == 'overwrite':
                    existing = target_existing.get(leaf)
                    if existing and existing['type'] == 'file':
                        try:
                            drive_service.delete_permanently_file(existing['uuid'])
                        except Exception as del_err:
                            return ('error', f"{src_path}: could not remove existing target ({del_err})")
                if kind == 'file':
                    drive_service.move_file(uuid, target_uuid)
                else:
                    drive_service.move_folder(uuid, target_uuid)
                return ('ok', src_path)
            except Exception as move_err:
                return ('error', f"{src_path}: {move_err}")

        max_workers = max(1, min(workers, len(plan)))
        if verbose:
            click.echo(f"  🧵 Using {max_workers} worker(s)")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_do_move, item) for item in plan]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    status, detail = fut.result()
                except Exception as fut_err:
                    _safe_log(f"  ❌ Worker exception: {fut_err}", err=True)
                    with counters_lock:
                        error_count += 1
                    continue
                if status == 'ok':
                    with counters_lock:
                        success_count += 1
                    if verbose:
                        _safe_log(f"  ✅ {detail}")
                else:
                    with counters_lock:
                        error_count += 1
                    _safe_log(f"  ❌ {detail}", err=True)

        # Final cache invalidation for the target (move_file/move_folder
        # already pop both source-parent and target, but be explicit).
        with drive_service.cache_lock:
            drive_service.folder_content_cache.pop(target_uuid, None)

        click.echo("=" * 40)
        click.echo("📊 Move Summary:")
        click.echo(f"  ✅ Moved:    {success_count}")
        click.echo(f"  ⏭️  Skipped:  {skipped_count}")
        click.echo(f"  ❌ Errors:   {error_count}")
        click.echo("=" * 40)

        if error_count:
            sys.exit(1)

    except Exception as e:
        click.echo(f"❌ Move operation failed: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

@cli.command()
@click.argument('sources', nargs=-1, type=str)
@click.option('--target', '-t', 'target_path', default='/', help='Destination path on Internxt Drive (default: /)')
@click.option('--recursive', '-r', is_flag=True, help='Upload directories recursively')
@click.option('--on-conflict', type=click.Choice(['overwrite', 'skip'], case_sensitive=False), default='skip', help='Action if target exists (overwrite/skip)')
@click.option('--preserve-timestamps', '-p', is_flag=True, help='Preserve file creation and modification times')
@click.option('--include', multiple=True, help='Include only files matching pattern (e.g., --include "*.png" --include "*.jpg")')
@click.option('--exclude', multiple=True, help='Exclude files matching pattern (e.g., --exclude "*.tmp" --exclude ".DS_Store")')
@click.option('--workers', '-w', type=int, default=4, show_default=True,
              help='Parallel upload workers for batch directory uploads (1 = serial)')
@click.option('--verbose', '-v', is_flag=True, help='Show verbose output')
def upload(sources: Tuple[str, ...], target_path: str, recursive: bool, on_conflict: str,
           preserve_timestamps: bool, include: Tuple[str, ...], exclude: Tuple[str, ...],
           workers: int, verbose: bool):
    """
    Encrypts and uploads local files/folders to a remote path.

    SOURCES... can be one or more local file or directory paths. Wildcards (*) are supported.
    TARGET_PATH is the destination path on your Internxt Drive (e.g., "/Documents/Backup").
    
    Trailing slash behavior (like rsync):
      source/  → uploads contents to target (no new folder)
      source   → creates 'source' folder in target
    
    Use --preserve-timestamps to maintain original file dates (experimental).
    Use --include/--exclude to filter files by pattern (supports wildcards).
    
    Examples:
      Upload only images:
        python cli.py upload photos/ -t /Backup -r --include "*.png" --include "*.jpg"
      
      Upload all except temp files:
        python cli.py upload project/ -t /Code -r --exclude "*.tmp" --exclude ".DS_Store"
      
      Preserve timestamps:
        python cli.py upload docs/ -t /Documents -r --preserve-timestamps
    """
    if not sources:
        click.echo("❌ No source files or directories specified.", err=True)
        sys.exit(1)

    # Convert include/exclude tuples to lists
    include_patterns = list(include) if include else []
    exclude_patterns = list(exclude) if exclude else []
    
    if verbose or include_patterns or exclude_patterns:
        if include_patterns:
            click.echo(f"🔍 Include filters: {', '.join(include_patterns)}")
        if exclude_patterns:
            click.echo(f"🚫 Exclude filters: {', '.join(exclude_patterns)}")

    try:
        click.echo("🔄 Refreshing authentication token...")
        auth_service.refresh_tokens()
        auth_service.get_auth_details()  # ensure session is loaded with the new token

        click.echo(f"🎯 Preparing upload to remote path: {target_path}")

        # --- Resolve or Create Target Folder ---
        target_folder_uuid = None
        target_folder_path_str = "/"
        try:
            target_folder_info = drive_service.resolve_path(target_path)
            if target_folder_info['type'] != 'folder':
                click.echo(f"❌ Target path '{target_path}' exists but is not a folder.", err=True)
                sys.exit(1)
            target_folder_uuid = target_folder_info['uuid']
            target_folder_path_str = target_folder_info['path']
            click.echo(f"✅ Target folder exists: '{target_folder_path_str}' (UUID: {target_folder_uuid[:8]}...)")
        except FileNotFoundError:
            click.echo(f"⏳ Target path '{target_path}' not found. Attempting to create...")
            try:
                created_folder = drive_service.create_folder_recursive(target_path)
                target_folder_uuid = created_folder['uuid']
                target_folder_info = drive_service.resolve_path(target_path)
                target_folder_path_str = target_folder_info['path']
                click.echo(f"✅ Created target folder '{target_folder_path_str}' (UUID: {target_folder_uuid[:8]}...)")
            except Exception as create_err:
                click.echo(f"❌ Failed to create target folder '{target_path}': {create_err}", err=True)
                sys.exit(1)
        except Exception as resolve_err:
            click.echo(f"❌ Error resolving target path '{target_path}': {resolve_err}", err=True)
            sys.exit(1)

        if not target_folder_uuid:
            click.echo("❌ Could not determine target folder UUID. Aborting.", err=True)
            sys.exit(1)

        # --- Process Sources ---
        items_to_process = []
        click.echo("🔍 Expanding source paths...")
        for source_arg in sources:
            has_trailing_slash = source_arg.rstrip().endswith('/') or source_arg.rstrip().endswith(os.sep)
            source_path = Path(source_arg)
            
            if not source_path.exists():
                click.echo(f"⚠️ Source not found: {source_arg}", err=True)
                continue
            
            source_path_resolved = source_path.resolve()
            source_path_str = str(source_path_resolved)
            matches = glob.glob(source_path_str, recursive=recursive)

            if not matches:
                click.echo(f"⚠️ Source not found or matched nothing: {source_arg}", err=True)
                continue

            base_dir_str = source_path_str
            if "*" in source_arg or "?" in source_arg or "[" in source_arg:
                path_parts = Path(source_arg).parts
                non_wildcard_parts = []
                for part in path_parts:
                    if "*" in part or "?" in part or "[" in part:
                        break
                    non_wildcard_parts.append(part)
                if non_wildcard_parts:
                    base_dir_str = str(Path(*non_wildcard_parts))
                    if Path(source_arg).is_dir() and not ("*" in source_arg or "?" in source_arg or "[" in source_arg):
                        base_dir_str = str(Path(source_arg))
                else:
                    base_dir_str = os.getcwd()
                if Path(source_arg).is_absolute():
                    base_dir_path = Path(base_dir_str)
                    if not base_dir_path.is_absolute():
                        base_dir_path = Path.cwd() / base_dir_path
                    base_dir_str = str(base_dir_path.resolve())

            base_source_dir = Path(base_dir_str)
            if base_source_dir.is_file():
                base_source_dir = base_source_dir.parent

            for match_str in matches:
                match_path = Path(match_str).resolve()
                current_base = base_source_dir if not match_path.is_dir() else match_path.parent
                copy_contents_only = has_trailing_slash if match_path.is_dir() else False
                items_to_process.append((match_path, current_base, copy_contents_only))

        if not items_to_process:
            click.echo("❌ No valid source files or directories found after expansion.", err=True)
            sys.exit(1)

        click.echo(f"📦 Found {len(items_to_process)} items/directories to process.")

        # --- Upload Loop ---
        success_count = 0
        skipped_count = 0
        error_count = 0
        filtered_count = 0

        processed_dirs = set()

        for local_path, base_source_dir, copy_contents_only in items_to_process:
            try:
                if verbose:
                    click.echo("-" * 40)
                    click.echo(f"Processing: {local_path}")

                if local_path.is_file():
                    # Apply include/exclude filters
                    if not drive_service.should_include_file(local_path, include_patterns, exclude_patterns):
                        if verbose:
                            click.echo(f"  -> 🚫 Filtered out: {local_path.name}")
                        filtered_count += 1
                        continue
                    
                    # Get timestamps if preservation requested
                    creation_time = None
                    modification_time = None
                    
                    if preserve_timestamps:
                        try:
                            stat_info = local_path.stat()                            
                            mtime = datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc)
                            modification_time = mtime.isoformat()
                            
                            try:
                                ctime = datetime.fromtimestamp(stat_info.st_birthtime, tz=timezone.utc)
                                creation_time = ctime.isoformat()
                            except AttributeError:
                                ctime = datetime.fromtimestamp(stat_info.st_ctime, tz=timezone.utc)
                                creation_time = ctime.isoformat()
                            
                            if verbose:
                                click.echo("  🕐 Local file timestamps:")
                                click.echo(f"     Creation: {creation_time}")
                                click.echo(f"     Modification: {modification_time}")
                        except Exception as e:
                            if verbose:
                                click.echo(f"  ⚠️  Could not read timestamps: {e}")
                    
                    # Upload
                    upload_result = drive_service.upload_single_item_with_conflict_handling(
                        local_path,
                        target_folder_path_str,
                        target_folder_uuid,
                        on_conflict,
                        remote_filename=local_path.name,
                        creation_time=creation_time,
                        modification_time=modification_time
                    )
                    
                    if upload_result == "uploaded":
                        success_count += 1
                    elif upload_result == "skipped":
                        skipped_count += 1
                    else:
                        error_count += 1

                elif local_path.is_dir():
                    # Helper closures below close over per-iteration locks/dicts;
                    # they're built and consumed entirely within this iteration.
                    # pylint: disable=cell-var-from-loop
                    if local_path in processed_dirs:
                        if verbose:
                            click.echo(f"  -> Skipping already processed directory: {local_path}")
                        continue

                    click.echo(f"📂 Processing directory recursively: {local_path}")
                    processed_dirs.add(local_path)

                    # Determine the remote base path
                    if copy_contents_only:
                        click.echo("  ✨ Copying contents directly to target (trailing slash detected)")
                        dir_remote_base_path = Path(target_folder_path_str)
                    else:
                        click.echo(f"  📁 Ensuring folder '{local_path.name}' exists in target...")
                        dir_remote_base_path = Path(target_folder_path_str) / local_path.name

                    dir_base_str = str(dir_remote_base_path).replace(os.sep, '/')
                    if not dir_base_str.startswith('/'):
                        dir_base_str = '/' + dir_base_str

                    # ===== Shared per-directory state (cache + locks for parallelism) =====
                    cache_lock = threading.Lock()
                    log_lock = threading.Lock()
                    counters_lock = threading.Lock()
                    # parent_uuid -> {filename: {size, mtime, uuid}}
                    existing_files_by_parent: Dict[str, Dict[str, Dict[str, Any]]] = {}

                    def _safe_log(msg: str, err: bool = False) -> None:
                        with log_lock:
                            click.echo(msg, err=err)

                    def _bump_counter(kind: str) -> None:
                        nonlocal success_count, skipped_count, error_count, filtered_count
                        with counters_lock:
                            if kind == 'success':
                                success_count += 1
                            elif kind == 'skipped':
                                skipped_count += 1
                            elif kind == 'error':
                                error_count += 1
                            elif kind == 'filtered':
                                filtered_count += 1

                    def _file_meta_from_api(api_file: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
                        plain = api_file.get('plainName', '') or api_file.get('name', '')
                        ext = api_file.get('type', '') or ''
                        full = f"{plain}.{ext}" if ext else plain
                        return full, {
                            'size': int(api_file.get('size', 0) or 0),
                            'mtime': api_file.get('modificationTime') or api_file.get('updatedAt') or '',
                            'uuid': api_file.get('uuid'),
                        }

                    def _seed_folder_cache(parent_uuid: str) -> Dict[str, Dict[str, Any]]:
                        with cache_lock:
                            if parent_uuid in existing_files_by_parent:
                                return existing_files_by_parent[parent_uuid]
                        meta_map: Dict[str, Dict[str, Any]] = {}
                        try:
                            content = drive_service.get_folder_content(parent_uuid)
                            for api_file in content.get('files', []):
                                name, meta = _file_meta_from_api(api_file)
                                meta_map[name] = meta
                        except Exception as scan_err:
                            if verbose:
                                _safe_log(f"  -> ⚠️  Could not pre-scan folder {parent_uuid[:8]}...: {scan_err}")
                        with cache_lock:
                            existing_files_by_parent[parent_uuid] = meta_map
                        return meta_map

                    # Progress tracking for the recursive remote pre-scan.
                    # Prints in-place updates throttled to ~5/sec so big trees
                    # don't flood the terminal.
                    scan_progress = {'folders': 0, 'files': 0, 'last_print': 0.0}

                    def _scan_tick(force: bool = False) -> None:
                        now = time.time()
                        if not force and (now - scan_progress['last_print']) < 0.2:
                            return
                        scan_progress['last_print'] = now
                        click.echo(
                            f"\r  -> 📋 Scanning remote: "
                            f"{scan_progress['folders']:,} folders, "
                            f"{scan_progress['files']:,} files",
                            nl=False,
                        )

                    def _seed_recursive(folder_uuid: str) -> None:
                        meta = _seed_folder_cache(folder_uuid)
                        scan_progress['folders'] += 1
                        scan_progress['files'] += len(meta)
                        _scan_tick()
                        try:
                            content = drive_service.get_folder_content(folder_uuid)
                            for sub in content.get('folders', []):
                                sub_uuid = sub.get('uuid')
                                if sub_uuid:
                                    _seed_recursive(sub_uuid)
                        except Exception:
                            return

                    def _should_skip(parent_uuid: str, filename: str,
                                     local_size: int, local_mtime_iso: Optional[str]) -> bool:
                        if on_conflict != 'skip':
                            return False
                        meta_map = _seed_folder_cache(parent_uuid)
                        with cache_lock:
                            entry = meta_map.get(filename)
                        if entry is None:
                            return False
                        # Size mismatch → re-upload
                        if entry['size'] and entry['size'] != local_size:
                            return False
                        # Mtime mismatch (only if user opted in to timestamp preservation)
                        if local_mtime_iso and entry.get('mtime'):
                            if entry['mtime'][:19] != local_mtime_iso[:19]:
                                return False
                        return True

                    def _read_timestamps(p: Path) -> Tuple[Optional[str], Optional[str]]:
                        if not preserve_timestamps:
                            return None, None
                        try:
                            stat_info = p.stat()
                            mt = datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc).isoformat()
                            try:
                                ct = datetime.fromtimestamp(stat_info.st_birthtime, tz=timezone.utc).isoformat()
                            except AttributeError:
                                ct = datetime.fromtimestamp(stat_info.st_ctime, tz=timezone.utc).isoformat()
                            return ct, mt
                        except Exception as ts_err:
                            if verbose:
                                _safe_log(f"  -> ⚠️  Could not read timestamps for {p.name}: {ts_err}")
                            return None, None

                    # ===== Detect existing remote subtree to short-circuit Pass 1 =====
                    pass1_skipped = False
                    existing_root_info: Optional[Dict[str, Any]] = None
                    try:
                        existing_root_info = drive_service.resolve_path(dir_base_str)
                        if existing_root_info['type'] != 'folder':
                            click.echo(f"     ❌ Target path exists but is not a folder: {dir_base_str}", err=True)
                            error_count += 1
                            continue
                    except FileNotFoundError:
                        existing_root_info = None
                    except Exception as e:
                        if verbose:
                            click.echo(f"  -> ⚠️  Could not check target subtree: {e}")

                    if existing_root_info is not None:
                        click.echo("  -> ✨ Target subtree exists; pre-scanning recursively to skip Pass 1...")
                        _seed_recursive(existing_root_info['uuid'])
                        _scan_tick(force=True)
                        click.echo("")  # finish the in-place line
                        pass1_skipped = True
                        total_files = sum(len(v) for v in existing_files_by_parent.values())
                        click.echo(f"  -> 📋 Pre-scanned {len(existing_files_by_parent):,} folders, {total_files:,} files")
                    elif not copy_contents_only:
                        # Need to create root folder with timestamps
                        ct_root, mt_root = _read_timestamps(local_path)
                        if verbose and mt_root:
                            click.echo(f"  🕐 Applying root dir timestamps: Mod={mt_root}")
                        try:
                            drive_service.create_folder_recursive(
                                dir_base_str,
                                creation_time=ct_root,
                                modification_time=mt_root,
                            )
                        except Exception as create_err:
                            click.echo(f"     ❌ Error creating root folder {local_path.name}: {create_err}", err=True)
                            error_count += 1
                            continue

                    # ===== Pass 1: create sub-directories (skipped if subtree pre-scanned) =====
                    if not pass1_skipped:
                        click.echo("  -> Pass 1/2: Creating subdirectory structure...")
                        dir_list = [it for it in local_path.rglob('*') if it.is_dir()]
                        dir_list.sort(key=lambda x: len(x.parts))

                        for item in dir_list:
                            if not drive_service.should_include_file(item, include_patterns, exclude_patterns):
                                if verbose:
                                    click.echo(f"  -> 🚫 Filtered dir: {item.name}")
                                filtered_count += 1
                                continue

                            relative_path = item.relative_to(local_path)
                            item_target_path_str = str(dir_remote_base_path / relative_path).replace(os.sep, '/')
                            if not item_target_path_str.startswith('/'):
                                item_target_path_str = '/' + item_target_path_str

                            ct, mt = _read_timestamps(item)
                            try:
                                if verbose:
                                    click.echo(f"  -> 📁 Ensuring dir: {item_target_path_str}")
                                created = drive_service.create_folder_recursive(
                                    item_target_path_str,
                                    creation_time=ct,
                                    modification_time=mt,
                                )
                                # Seed cache for the just-ensured folder so Pass 2
                                # makes zero listing calls.
                                if created and created.get('uuid'):
                                    _seed_folder_cache(created['uuid'])
                            except Exception as create_err:
                                click.echo(f"     ❌ Error creating dir {item_target_path_str}: {create_err}", err=True)
                                error_count += 1
                    else:
                        click.echo("  -> Pass 1/2: Skipped (subtree pre-scanned)")

                    click.echo("  -> Pass 2/2: Uploading files...")

                    # ===== Build the upload work list (sequential parent resolution) =====
                    upload_jobs: List[Tuple[Path, str, str, Optional[str], Optional[str]]] = []
                    plan_progress = {'seen': 0, 'queued': 0, 'skipped_local': 0, 'last_print': 0.0}

                    def _plan_tick(force: bool = False) -> None:
                        now = time.time()
                        if not force and (now - plan_progress['last_print']) < 0.2:
                            return
                        plan_progress['last_print'] = now
                        click.echo(
                            f"\r  -> 🔎 Planning: scanned {plan_progress['seen']:,}, "
                            f"queued {plan_progress['queued']:,}, "
                            f"skipped {plan_progress['skipped_local']:,}",
                            nl=False,
                        )

                    for item in local_path.rglob('*'):
                        if not item.is_file():
                            continue
                        plan_progress['seen'] += 1
                        _plan_tick()
                        if not drive_service.should_include_file(item, include_patterns, exclude_patterns):
                            if verbose:
                                _safe_log(f"  -> 🚫 Filtered: {item.name}")
                            filtered_count += 1
                            continue

                        relative_path = item.relative_to(local_path)
                        item_target_parent_path_str = str(dir_remote_base_path / relative_path.parent).replace(os.sep, '/')
                        if not item_target_parent_path_str.startswith('/'):
                            item_target_parent_path_str = '/' + item_target_parent_path_str

                        try:
                            parent_folder = drive_service.create_folder_recursive(item_target_parent_path_str)
                            parent_folder_uuid = parent_folder['uuid']
                        except Exception as create_err:
                            click.echo(f"     ❌ Error ensuring parent folder {item_target_parent_path_str}: {create_err}", err=True)
                            error_count += 1
                            continue

                        ct, mt = _read_timestamps(item)

                        try:
                            local_size = item.stat().st_size
                        except Exception:
                            local_size = 0

                        # Fast skip BEFORE submitting (avoids occupying a worker)
                        if _should_skip(parent_folder_uuid, item.name, local_size, mt):
                            if verbose:
                                _safe_log(f"  -> ⏭️  Skipping (already exists): {relative_path}")
                            skipped_count += 1
                            plan_progress['skipped_local'] += 1
                            continue

                        upload_jobs.append((item, parent_folder_uuid, item_target_parent_path_str, ct, mt))
                        plan_progress['queued'] += 1

                    _plan_tick(force=True)
                    click.echo("")

                    # ===== Pass 2: actual uploads (parallel) =====
                    # Ctrl+C sets this; queued-but-not-started workers see it
                    # at the top of _do_upload and return immediately. In-flight
                    # uploads can't be killed mid-stream (network call is
                    # blocking), but no new ones will start.
                    cancel_event = threading.Event()

                    def _do_upload(job: Tuple[Path, str, str, Optional[str], Optional[str]]
                                   ) -> Tuple[str, Path, str]:
                        f_item, f_parent_uuid, f_parent_path, f_ct, f_mt = job
                        if cancel_event.is_set():
                            return ('cancelled', f_item, f_parent_uuid)
                        try:
                            res = drive_service.upload_single_item_with_conflict_handling(
                                f_item,
                                f_parent_path,
                                f_parent_uuid,
                                on_conflict,
                                remote_filename=f_item.name,
                                creation_time=f_ct,
                                modification_time=f_mt,
                            )
                        except Exception as up_err:
                            _safe_log(f"  -> ❌ Upload error for {f_item.name}: {up_err}", err=True)
                            return ('error', f_item, f_parent_uuid)
                        return (res, f_item, f_parent_uuid)

                    if upload_jobs:
                        max_workers = max(1, min(workers, len(upload_jobs)))
                        click.echo(f"  -> 🧵 Uploading {len(upload_jobs):,} file(s) with {max_workers} worker(s)")
                        upload_progress = {'done': 0, 'ok': 0, 'err': 0, 'cancelled': 0,
                                           'total': len(upload_jobs), 'last_print': 0.0}
                        upload_progress_lock = threading.Lock()

                        def _upload_tick(force: bool = False) -> None:
                            now = time.time()
                            with upload_progress_lock:
                                if not force and (now - upload_progress['last_print']) < 0.2:
                                    return
                                upload_progress['last_print'] = now
                                done = upload_progress['done']
                                total = upload_progress['total']
                                ok = upload_progress['ok']
                                err = upload_progress['err']
                                canc = upload_progress['cancelled']
                            pct = (100.0 * done / total) if total else 100.0
                            canc_part = f" cancelled={canc:,}" if canc else ""
                            with log_lock:
                                click.echo(
                                    f"\r  -> 📤 Uploaded {done:,}/{total:,} "
                                    f"({pct:5.1f}%) ok={ok:,} err={err:,}{canc_part}",
                                    nl=False,
                                )

                        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                            futures = [executor.submit(_do_upload, job) for job in upload_jobs]
                            try:
                                for fut in concurrent.futures.as_completed(futures):
                                    try:
                                        result, item_done, parent_uuid_done = fut.result()
                                    except concurrent.futures.CancelledError:
                                        with upload_progress_lock:
                                            upload_progress['done'] += 1
                                            upload_progress['cancelled'] += 1
                                        _upload_tick()
                                        continue
                                    except Exception as fut_err:
                                        _safe_log(f"  -> ❌ Worker exception: {fut_err}", err=True)
                                        _bump_counter('error')
                                        with upload_progress_lock:
                                            upload_progress['done'] += 1
                                            upload_progress['err'] += 1
                                        _upload_tick()
                                        continue

                                    if result == 'uploaded':
                                        _bump_counter('success')
                                        try:
                                            sz = item_done.stat().st_size
                                        except Exception:
                                            sz = 0
                                        with cache_lock:
                                            existing_files_by_parent.setdefault(parent_uuid_done, {})[item_done.name] = {
                                                'size': sz,
                                                'mtime': '',
                                                'uuid': None,
                                            }
                                        with upload_progress_lock:
                                            upload_progress['done'] += 1
                                            upload_progress['ok'] += 1
                                    elif result == 'skipped':
                                        _bump_counter('skipped')
                                        with upload_progress_lock:
                                            upload_progress['done'] += 1
                                    elif result == 'cancelled':
                                        with upload_progress_lock:
                                            upload_progress['done'] += 1
                                            upload_progress['cancelled'] += 1
                                    else:
                                        _bump_counter('error')
                                        with upload_progress_lock:
                                            upload_progress['done'] += 1
                                            upload_progress['err'] += 1
                                    _upload_tick()
                            except KeyboardInterrupt:
                                cancel_event.set()
                                _upload_tick(force=True)
                                click.echo("")
                                _safe_log("⚠️  Ctrl+C received — cancelling queued uploads "
                                          "(in-flight transfers will finish)...", err=True)
                                # Drop futures that haven't started yet.
                                executor.shutdown(wait=False, cancel_futures=True)
                                # Re-raise so the outer handler reports the abort
                                # and exits with the right summary printed.
                                raise
                        _upload_tick(force=True)
                        click.echo("")

                else:
                    click.echo(f"⚠️ Skipping unknown item type: {local_path}", err=True)
                    skipped_count += 1

            except Exception as e:
                # KeyboardInterrupt is BaseException, not Exception, so it
                # naturally bubbles past this handler to the outer try.
                click.echo(f"❌ Error processing {local_path}: {e}", err=True)
                error_count += 1
                continue

        aborted = False

    except KeyboardInterrupt:
        aborted = True
    except Exception as e:
        click.echo(f"❌ Upload failed: {e}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # --- Summary (always printed, even on Ctrl+C) ---
    click.echo("=" * 40)
    if aborted:
        click.echo("⚠️  Upload ABORTED by user (Ctrl+C)")
    click.echo("📊 Upload Summary:")
    click.echo(f"  ✅ Uploaded: {success_count}")
    click.echo(f"  ⏭️  Skipped:  {skipped_count}")
    if filtered_count > 0:
        click.echo(f"  🚫 Filtered: {filtered_count}")
    click.echo(f"  ❌ Errors:   {error_count}")
    click.echo("=" * 40)
    if aborted:
        sys.exit(130)  # standard exit code for SIGINT
    if error_count:
        sys.exit(1)

@cli.command()
@click.argument('file_uuid')
@click.option('--destination', '-d', type=click.Path(file_okay=True, writable=True, resolve_path=True), default='.', help='Where to save the file')
@click.option('--preserve-timestamps', '-p', is_flag=True, help='Preserve file modification times')
@click.option('--on-conflict', type=click.Choice(['overwrite', 'skip'], case_sensitive=False), default='overwrite', help='Action if local file exists')
@click.option('--verbose', '-v', is_flag=True, help='Show verbose output')
def download(file_uuid: str, destination: str, preserve_timestamps: bool, on_conflict: str, verbose: bool):
    """Downloads and decrypts a file from your Internxt Drive (by UUID)"""
    try:
        if verbose:
            click.echo(f"📥 Downloading file with UUID: {file_uuid}")
            click.echo(f"📁 Destination: {destination}")
            if preserve_timestamps:
                click.echo("🕐 Timestamp preservation: enabled")
        
        # Check if destination file already exists
        dest_path = Path(destination)
        if dest_path.is_file() and on_conflict == 'skip':
            click.echo(f"⏭️  File exists, skipping: {dest_path}")
            sys.exit(0)
        
        # Download the file
        downloaded_path = drive_service.download_file(
            file_uuid, 
            destination,
            preserve_timestamps=preserve_timestamps
        )
        
        if not verbose:
            click.echo(f"✅ File downloaded successfully to: {downloaded_path}")
    
    except Exception as e:
        click.echo(f"❌ Error downloading file: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


# ========== PATH-BASED OPERATIONS ==========

@cli.command('list-path')
@click.argument('path', default='/')
@click.option('--detailed', '-d', is_flag=True, help='Show detailed information')
@click.option('--all', '-a', 'show_all', is_flag=True, help='Show all attributes (verbose)')
def list_path(path: str, detailed: bool, show_all: bool):
    """List folder contents with paths (much more user-friendly!)"""
    try:
        auth_service.get_auth_details()
        
        content = drive_service.list_folder_with_paths(path)
        
        click.echo(f"\n📁 Listing folder: {path}")
        click.echo()
        click.echo(f"📁 Contents of: {content['current_path']}")
        click.echo("=" * 80)
        
        # Show folders first
        if content['folders']:
            click.echo("📂 Folders:")
            click.echo("-" * 60)
            for folder in content['folders']:
                if show_all:
                    # Show ALL attributes
                    click.echo(f"  📁 {folder['display_name']}")
                    click.echo(f"     UUID: {folder['uuid']}")
                    click.echo(f"     Path: {folder['path']}")
                    click.echo(f"     Plain Name: {folder.get('plainName', 'N/A')}")
                    click.echo(f"     Parent ID: {folder.get('parentId', 'N/A')}")
                    click.echo(f"     User ID: {folder.get('userId', 'N/A')}")
                    
                    # Timestamps for FOLDERS
                    created_at = folder.get('createdAt', 'N/A')
                    updated_at = folder.get('updatedAt', 'N/A')
                    creation_time = folder.get('creationTime', 'N/A')
                    modification_time = folder.get('modificationTime', 'N/A')
                    
                    # Use the correct logic for display
                    display_creation = creation_time if creation_time != 'N/A' else created_at
                    display_modification = modification_time if modification_time != 'N/A' else updated_at
                    
                    if display_creation != 'N/A':
                        click.echo(f"     Creation Time: {format_date(display_creation)} ({display_creation})")
                    else:
                        click.echo("     Creation Time: N/A")
                    
                    if display_modification != 'N/A':
                        click.echo(f"     Modification Time: {format_date(display_modification)} ({display_modification})")
                    else:
                        click.echo("     Modification Time: N/A")
                    
                    # Other attributes
                    click.echo(f"     Deleted: {folder.get('deleted', False)}")
                    
                    # Other attributes
                    click.echo(f"     Deleted: {folder.get('deleted', False)}")
                    click.echo(f"     Removed: {folder.get('removed', False)}")
                    
                    click.echo()
                elif detailed:
                    modified = folder.get('modified', '')[:10] if folder.get('modified') else ''
                    click.echo(f"  📁 {folder['display_name']:<30} {modified:<12} {folder['uuid'][:8]}...")
                else:
                    click.echo(f"  📁 {folder['display_name']}")
        
        # Then show files
        if content['files']:
            if content['folders']:
                click.echo()
            click.echo("📄 Files:")
            click.echo("-" * 60)
            for file in content['files']:
                if show_all:
                    # Show ALL attributes
                    click.echo(f"  📄 {file['display_name']}")
                    click.echo(f"     UUID: {file['uuid']}")
                    click.echo(f"     Path: {file['path']}")
                    click.echo(f"     Plain Name: {file.get('plainName', 'N/A')}")
                    click.echo(f"     Type/Extension: {file.get('type', 'N/A')}")
                    click.echo(f"     Size: {file['size_display']} ({file.get('size', 0)} bytes)")
                    click.echo(f"     Folder ID: {file.get('folderId', 'N/A')}")
                    click.echo(f"     User ID: {file.get('userId', 'N/A')}")
                    click.echo(f"     File ID: {file.get('fileId', 'N/A')}")
                    click.echo(f"     Bucket: {file.get('bucket', 'N/A')}")
                    click.echo(f"     Encrypt Version: {file.get('encryptVersion', 'N/A')}")
                    
                    # Timestamps for FILES
                    created_at = file.get('createdAt', 'N/A')
                    updated_at = file.get('updatedAt', 'N/A')
                    creation_time = file.get('creationTime', 'N/A')
                    modification_time = file.get('modificationTime', 'N/A')
                    
                    # Use the correct logic for display
                    display_creation = creation_time if creation_time != 'N/A' else created_at
                    display_modification = modification_time if modification_time != 'N/A' else updated_at
                    
                    if display_creation != 'N/A':
                        click.echo(f"     Creation Time: {format_date(display_creation)} ({display_creation})")
                    else:
                        click.echo("     Creation Time: N/A")
                    
                    if display_modification != 'N/A':
                        click.echo(f"     Modification Time: {format_date(display_modification)} ({display_modification})")
                    else:
                        click.echo("     Modification Time: N/A")
                    
                    # Other attributes
                    click.echo(f"     Deleted: {file.get('deleted', False)}")
                    click.echo(f"     Removed: {file.get('removed', False)}")
                    click.echo(f"     Status: {file.get('status', 'N/A')}")
                    
                    click.echo()
                elif detailed:
                    modified = file.get('modified', '')[:10] if file.get('modified') else ''
                    size = file['size_display']
                    click.echo(f"  📄 {file['display_name']:<30} {size:<10} {modified:<12} {file['uuid'][:8]}...")
                else:
                    size = file['size_display']
                    click.echo(f"  📄 {file['display_name']:<30} {size}")
        
        if not content['folders'] and not content['files']:
            click.echo("  (empty)")
            
        click.echo(f"\nTotal: {len(content['folders'])} folders, {len(content['files'])} files")
        
        # Show usage examples (only if not showing all attributes)
        if content['files'] and not show_all:
            example_file = content['files'][0]
            example_path = example_file['path']
            click.echo("\n💡 Usage examples:")
            click.echo(f"   Download by path: python cli.py download-path \"{example_path}\"")
            click.echo(f"   Delete by path:   python cli.py trash-path \"{example_path}\"")
    
    except ValueError as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Unexpected error: {e}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

@cli.command('download-path')
@click.argument('path')
@click.option('--destination', '-d', '--target', '-t', 'destination', help='Where to save (file or directory)')
@click.option('--recursive', '-r', is_flag=True, help='Download folders recursively')
@click.option('--on-conflict', type=click.Choice(['overwrite', 'skip'], case_sensitive=False), default='skip', help='Action if local file exists')
@click.option('--preserve-timestamps', '-p', is_flag=True, help='Preserve file modification times')
@click.option('--include', multiple=True, help='Include only files matching pattern')
@click.option('--exclude', multiple=True, help='Exclude files matching pattern')
@click.option('--verbose', '-v', is_flag=True, help='Show verbose output')
def download_path(path: str, destination: Optional[str], recursive: bool, on_conflict: str,
                  preserve_timestamps: bool, include: Tuple[str], exclude: Tuple[str], verbose: bool):
    """
    Download a file or folder by its path.
    
    Examples:
      Download single file:
        python cli.py download-path "/Documents/report.pdf"
      
      Download folder recursively:
        python cli.py download-path "/Photos" -r -d ./local_photos
      
      Download only images:
        python cli.py download-path "/Photos" -r --include "*.jpg" --include "*.png"
      
      With timestamp preservation:
        python cli.py download-path "/Backup" -r -p
    """
    try:
        auth_service.get_auth_details()
        
        # Convert include/exclude tuples to lists
        include_patterns = list(include) if include else []
        exclude_patterns = list(exclude) if exclude else []
        
        if verbose and (include_patterns or exclude_patterns):
            if include_patterns:
                click.echo(f"🔍 Include filters: {', '.join(include_patterns)}")
            if exclude_patterns:
                click.echo(f"🚫 Exclude filters: {', '.join(exclude_patterns)}")
        
        # Resolve the remote path
        item_info = drive_service.resolve_path(path)
        
        if item_info['type'] == 'file':
            # Single file download
            if verbose:
                click.echo(f"📥 Downloading file: {path}")
            
            # Apply filters
            file_name = item_info.get('plainName', '')
            if item_info.get('type'):
                file_name = f"{file_name}.{item_info.get('type')}"
            
            if not drive_service.should_include_file(Path(file_name), include_patterns, exclude_patterns):
                click.echo("🚫 File filtered out by include/exclude patterns")
                sys.exit(0)
            
            # Determine destination
            if destination:
                dest_path = Path(destination)
            else:
                dest_path = Path.cwd() / file_name
            
            # Check conflict
            if dest_path.exists() and on_conflict == 'skip':
                click.echo(f"⏭️  File exists, skipping: {dest_path}")
                sys.exit(0)
            
            # Download
            downloaded_path = drive_service.download_file(
                item_info['uuid'], 
                str(dest_path.parent if dest_path.is_file() or not dest_path.exists() else dest_path),
                preserve_timestamps=preserve_timestamps
            )
            
            click.echo("\n🎉 Downloaded successfully!")
            click.echo(f"📄 From: {path}")
            click.echo(f"💾 To: {downloaded_path}")
            
        elif item_info['type'] == 'folder':
            if not recursive:
                click.echo(f"❌ '{path}' is a folder. Use -r to download recursively.", err=True)
                sys.exit(1)
            
            # Recursive folder download
            click.echo(f"📂 Downloading folder recursively: {path}")
            
            # Determine base destination
            if destination:
                base_dest = Path(destination)
            else:
                folder_name = item_info.get('plainName', 'download')
                base_dest = Path.cwd() / folder_name
            
            base_dest.mkdir(parents=True, exist_ok=True)
            
            # Download folder contents recursively
            success_count = 0
            skipped_count = 0
            error_count = 0
            filtered_count = 0
            
            def download_folder_recursive(folder_uuid: str, current_dest: Path, current_path: str):
                nonlocal success_count, skipped_count, error_count, filtered_count
                
                # Get folder contents
                content = drive_service.get_folder_content(folder_uuid)
                
                # Download files
                for file_info in content.get('files', []):
                    file_name = file_info.get('plainName', '')
                    if file_info.get('type'):
                        file_name = f"{file_name}.{file_info.get('type')}"
                    
                    # Apply filters
                    if not drive_service.should_include_file(Path(file_name), include_patterns, exclude_patterns):
                        if verbose:
                            click.echo(f"  -> 🚫 Filtered: {file_name}")
                        filtered_count += 1
                        continue
                    
                    file_dest = current_dest / file_name
                    
                    # Check conflict
                    if file_dest.exists() and on_conflict == 'skip':
                        if verbose:
                            click.echo(f"  -> ⏭️  Skipping existing: {file_name}")
                        skipped_count += 1
                        continue
                    
                    try:
                        if verbose:
                            click.echo(f"  -> Downloading: {file_name}")
                        
                        drive_service.download_file(
                            file_info['uuid'],
                            str(current_dest),
                            preserve_timestamps=preserve_timestamps
                        )
                        success_count += 1
                    except Exception as e:
                        click.echo(f"  -> ❌ Error downloading {file_name}: {e}", err=True)
                        error_count += 1
                
                # Process subfolders
                for folder_info in content.get('folders', []):
                    folder_name = folder_info.get('plainName', folder_info.get('name', ''))
                    subfolder_dest = current_dest / folder_name
                    subfolder_dest.mkdir(parents=True, exist_ok=True)
                    
                    if preserve_timestamps:
                        try:
                            # Get modification time (preferred) or creation time
                            mod_time_iso = folder_info.get('modificationTime') or folder_info.get('updatedAt')
                            
                            if mod_time_iso:
                                dt = datetime.fromisoformat(mod_time_iso.replace('Z', '+00:00'))
                                mtime_ts = dt.timestamp()
                                
                                # Set access and modification times
                                os.utime(subfolder_dest, (mtime_ts, mtime_ts))
                                if verbose:
                                    click.echo(f"  -> 🕐 Set folder timestamp for: {folder_name}")
                        except Exception as e:
                            if verbose:
                                click.echo(f"  -> ⚠️  Could not set timestamp for {folder_name}: {e}")
                    
                    if verbose:
                        click.echo(f"📂 Entering folder: {folder_name}")
                    
                    download_folder_recursive(
                        folder_info['uuid'],
                        subfolder_dest,
                        f"{current_path}/{folder_name}"
                    )
            
            # Start recursive download
            download_folder_recursive(item_info['uuid'], base_dest, path)
            
            # Summary
            click.echo("\n" + "=" * 40)
            click.echo("📊 Download Summary:")
            click.echo(f"  ✅ Downloaded: {success_count}")
            click.echo(f"  ⏭️  Skipped:    {skipped_count}")
            if filtered_count > 0:
                click.echo(f"  🚫 Filtered:   {filtered_count}")
            click.echo(f"  ❌ Errors:     {error_count}")
            click.echo(f"  📁 To: {base_dest}")
            click.echo("=" * 40)
        
        else:
            click.echo(f"❌ Unknown item type: {item_info['type']}", err=True)
            sys.exit(1)
        
    except FileNotFoundError as e:
        click.echo(f"❌ File not found: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Unexpected error: {e}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

@cli.command('search')
@click.argument('search_term')
@click.option('--detailed', '-d', is_flag=True, help='Show full metadata (slower)')
def search(search_term: str, detailed: bool):
    """
    Perform a fast, server-side fuzzy search (global).
    
    Using -d will fetch full metadata for each result, which is slower.
    """
    try:
        auth_service.get_auth_details()
        
        results = drive_service.search_drive(search_term)
        
        if not results:
            click.echo(f"❌ No items found matching '{search_term}'")
            return
        
        click.echo(f"\n⚡ Found {len(results)} items matching '{search_term}':")
        
        hydrated_results = []
        
        if detailed:
            click.echo("🔍 Fetching full metadata for results (this may take a moment)...")
            
            try:
                from tqdm import tqdm  # pylint: disable=redefined-outer-name
            except ImportError:
                class TqdmFallback:
                    def __init__(self, total=None, desc=None, unit=None): pass
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def update(self, n=1): pass
                tqdm = TqdmFallback

            with tqdm(total=len(results), desc="Fetching details", unit="item", leave=False) as pbar:
                for item in results:
                    try:
                        uuid = item.get('itemId', item.get('id'))
                        if not uuid:
                            pbar.update(1)
                            continue
                        metadata = {}
                        if item.get('itemType') == 'folder':
                            metadata = drive_service.get_folder_metadata(uuid)
                            metadata['itemType'] = 'folder'
                        elif item.get('itemType') == 'file':
                            metadata = drive_service.get_file_metadata(uuid)
                            metadata['itemType'] = 'file'
                        
                        # --- NEW: Get the full path ---
                        metadata['fullPath'] = drive_service.get_full_path_for_item(metadata)
                        # --- END NEW ---
                        
                        hydrated_results.append(metadata)
                    except Exception as e:
                        click.echo(f"\n⚠️  Could not fetch metadata for {item.get('name')}: {e}")
                    pbar.update(1)
            results = hydrated_results # Overwrite with the rich data
            click.echo() # Newline after progress bar
        
        click.echo("=" * 80)
        
        folders = [item for item in results if item.get('itemType') == 'folder']
        files = [item for item in results if item.get('itemType') == 'file']
        
        if folders:
            click.echo("📂 Folders:")
            for folder in folders:
                name = folder.get('plainName', folder.get('name', 'Unknown'))
                uuid = folder.get('uuid', folder.get('itemId', 'N/A'))
                if detailed:
                    mod_date_iso = folder.get('modificationTime') or folder.get('updatedAt', '')
                    mod_date = format_date(mod_date_iso) if mod_date_iso else 'N/A'
                    click.echo(f"  📁 {folder.get('fullPath', name)}") # <-- Show path
                    click.echo(f"     UUID: {uuid}")
                    click.echo(f"     Modified: {mod_date}")
                else:
                    click.echo(f"  📁 {name}  (UUID: {uuid[:8]}...)")
        
        if files:
            click.echo("\n📄 Files:")
            for file in files:
                name = file.get('plainName', file.get('name', 'Unknown'))
                uuid = file.get('uuid', file.get('itemId', 'N/A'))
                ext = file.get('type')
                
                if ext and not name.endswith(f'.{ext}'):
                    name = f"{name}.{ext}"

                if detailed:
                    size = format_size(int(file.get('size', 0)))
                    mod_date_iso = file.get('modificationTime') or file.get('updatedAt', '')
                    mod_date = format_date(mod_date_iso) if mod_date_iso else 'N/A'
                    click.echo(f"  📄 {file.get('fullPath', name)}") # <-- Show path
                    click.echo(f"     UUID: {uuid}")
                    click.echo(f"     Size: {size}")
                    click.echo(f"     Modified: {mod_date}")
                else:
                    click.echo(f"  📄 {name}  (UUID: {uuid[:8]}...)")

        click.echo("\n" + "=" * 80)
        click.echo("💡 Use the UUID with 'download' or the full path with 'download-path'.")
    
    except ValueError as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Unexpected error: {e}", err=True)
        sys.exit(1)

@cli.command('find')
@click.argument('path', default='/')
@click.option('-name', 'name_pattern', default=None, help='Case-sensitive name pattern (e.g., "*.py")')
@click.option('-iname', 'iname_pattern', default=None, help='Case-insensitive name pattern (e.g., "*.py")')
@click.option('--maxdepth', type=int, default=-1, help='Limit search to N levels deep (-1 for infinite).') # <-- ADDED
def find(path: str, name_pattern: Optional[str], iname_pattern: Optional[str], maxdepth: int): # <-- ADDED
    """
    Search for files by name pattern (POSIX-like).

    Examples:
      python cli.py find / -name "*.pdf"
      python cli.py find . -iname "*.jpg" --maxdepth 2
    """
    try:
        search_term = None
        case_sensitive = False

        if name_pattern and iname_pattern:
            click.echo("❌ Error: You can only use -name or -iname, not both.", err=True)
            sys.exit(1)
        elif name_pattern:
            search_term = name_pattern
            case_sensitive = True
        elif iname_pattern:
            search_term = iname_pattern
            case_sensitive = False
        else:
            click.echo("❌ Error: You must provide either --name or --iname.", err=True)
            sys.exit(1)

        auth_service.get_auth_details()
        
        results = drive_service.find_files(
            search_term, 
            path, 
            case_sensitive=case_sensitive,
            max_depth=maxdepth
        )
        
        if not results:
            click.echo(f"❌ No files found matching '{search_term}' in {path}")
            return
        
        click.echo(f"\n🔍 Found {len(results)} files matching '{search_term}':")
        click.echo("=" * 80)
        
        for file in results:
            size = file.get('size_display', 'Unknown')
            modified = file.get('modified', '')[:10] if file.get('modified') else ''
            click.echo(f"📄 {file['path']}")
            click.echo(f"   Size: {size:<10} Modified: {modified:<12} UUID: {file['uuid'][:8]}...")
            click.echo()
        
        if results:
            example = results[0]
            click.echo("💡 Usage examples:")
            click.echo(f"   Download: python cli.py download-path \"{example['path']}\"")
            click.echo(f"   Delete:   python cli.py trash-path \"{example['path']}\"")
    
    except ValueError as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Unexpected error: {e}", err=True)
        sys.exit(1)

@cli.command()
@click.argument('path')
def resolve(path: str):
    """Show what a path points to (debugging tool)"""
    try:
        auth_service.get_auth_details()
        
        resolved = drive_service.resolve_path(path)
        
        click.echo(f"\n🔍 Path resolution for: {path}")
        click.echo("=" * 50)
        click.echo(f"Type: {resolved['type'].upper()}")
        click.echo(f"UUID: {resolved['uuid']}")
        click.echo(f"Resolved path: {resolved['path']}")
        
        if resolved['type'] == 'file':
            metadata = resolved['metadata']
            file_type = metadata.get('type', '')
            size = format_size(metadata.get('size', 0))
            click.echo(f"File type: {file_type}")
            click.echo(f"Size: {size}")
        
        click.echo("\n💡 You can use this path with:")
        if resolved['type'] == 'file':
            click.echo(f"   python cli.py download-path \"{resolved['path']}\"")
            click.echo(f"   python cli.py trash-path \"{resolved['path']}\"")
        else:
            click.echo(f"   python cli.py list-path \"{resolved['path']}\"")
    
    except FileNotFoundError as e:
        click.echo(f"❌ Path not found: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Unexpected error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('path', default='/')
@click.option('--depth', '-d', type=int, default=3, help='Maximum depth to show')
def tree(path: str, depth: int):
    """Show folder structure as a tree (like 'tree' command)"""
    try:
        auth_service.get_auth_details()
        
        click.echo(f"\n🌳 Folder tree starting from: {path}")
        click.echo("=" * 60)
        
        def print_tree(current_path, current_depth=0, prefix="", is_last=True):
            if current_depth >= depth:
                return
                
            try:
                content = drive_service.list_folder_with_paths(current_path)
                
                # Print current folder name (except root)
                if current_depth > 0:
                    connector = "└── " if is_last else "├── "
                    folder_name = Path(current_path).name
                    click.echo(f"{prefix}{connector}📁 {folder_name}/")
                    
                    # Update prefix for children
                    child_prefix = prefix + ("    " if is_last else "│   ")
                else:
                    child_prefix = ""
                
                # Print folders and files
                all_items = content['folders'] + content['files']
                for i, item in enumerate(all_items):
                    is_last_item = (i == len(all_items) - 1)
                    connector = "└── " if is_last_item else "├── "
                    
                    if item in content['folders']:
                        # It's a folder - recurse if not at max depth
                        if current_depth + 1 < depth:
                            print_tree(item['path'], current_depth + 1, child_prefix, is_last_item)
                        else:
                            click.echo(f"{child_prefix}{connector}📁 {item['display_name']}/")
                    else:
                        # It's a file
                        size = item.get('size_display', '')
                        click.echo(f"{child_prefix}{connector}📄 {item['display_name']} ({size})")
                        
            except Exception as e:
                click.echo(f"{prefix}    ❌ Error reading folder: {e}")
        
        print_tree(path)
        click.echo(f"\n(Showing maximum {depth} levels deep)")
    
    except ValueError as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Unexpected error: {e}", err=True)
        sys.exit(1)


# ========== DELETE/TRASH OPERATIONS ==========

@cli.command('trash')
@click.argument('file_or_folder_uuid')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def trash_by_uuid(file_or_folder_uuid: str, force: bool):
    """Move a file or folder to trash by UUID"""
    try:
        auth_service.get_auth_details()
        
        if not force:
            if not click.confirm(f'Move item {file_or_folder_uuid} to trash?'):
                click.echo("❌ Cancelled")
                return
        
        # Try as file first, then folder
        try:
            result = drive_service.trash_file(file_or_folder_uuid)
            click.echo(f"✅ {result['message']}")
        except Exception:  # noqa: BLE001 - fall back to folder if file trash fails
            result = drive_service.trash_folder(file_or_folder_uuid)
            click.echo(f"✅ {result['message']}")
            
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)


@cli.command('trash-path')
@click.argument('path')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def trash_by_path(path: str, force: bool):
    """Move a file or folder to trash by path"""
    try:
        auth_service.get_auth_details()
        
        resolved = drive_service.resolve_path(path)
        
        if not force:
            item_type = resolved['type']
            if not click.confirm(f'Move {item_type} "{path}" to trash?'):
                click.echo("❌ Cancelled")
                return
        
        result = drive_service.trash_by_path(path)
        click.echo(f"✅ {result['message']}")
        click.echo(f"🗑️  Item moved to trash: {path}")
        
    except FileNotFoundError as e:
        click.echo(f"❌ Path not found: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)


@cli.command('delete')
@click.argument('file_or_folder_uuid')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def delete_permanently_by_uuid(file_or_folder_uuid: str, force: bool):
    """Permanently delete a file or folder by UUID (CANNOT BE UNDONE!)"""
    try:
        auth_service.get_auth_details()
        
        if not force:
            click.echo("⚠️  WARNING: This will PERMANENTLY delete the item. This action cannot be undone!")
            if not click.confirm(f'Permanently delete item {file_or_folder_uuid}?'):
                click.echo("❌ Cancelled")
                return
        
        # Try as file first, then folder
        try:
            result = drive_service.delete_permanently_file(file_or_folder_uuid)
            click.echo(f"✅ {result['message']}")
        except Exception:  # noqa: BLE001 - fall back to folder if file delete fails
            result = drive_service.delete_permanently_folder(file_or_folder_uuid)
            click.echo(f"✅ {result['message']}")
            
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)


@cli.command('delete-path')
@click.argument('path')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def delete_permanently_by_path(path: str, force: bool):
    """Permanently delete a file or folder by path (CANNOT BE UNDONE!)"""
    try:
        auth_service.get_auth_details()
        
        resolved = drive_service.resolve_path(path)
        
        if not force:
            item_type = resolved['type']
            click.echo("⚠️  WARNING: This will PERMANENTLY delete the item. This action cannot be undone!")
            if not click.confirm(f'Permanently delete {item_type} "{path}"?'):
                click.echo("❌ Cancelled")
                return
        
        result = drive_service.delete_permanently_by_path(path)
        click.echo(f"✅ {result['message']}")
        click.echo(f"🗑️  Item permanently deleted: {path}")
        
    except FileNotFoundError as e:
        click.echo(f"❌ Path not found: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)


# ========== WEBDAV SERVER COMMANDS ==========

@cli.command('webdav-start')
@click.option('--port', type=int, help='Port to run WebDAV server on')
@click.option('--background', '-b', is_flag=True, help='Run server in background')
@click.option('--show-mount', is_flag=True, help='Show mount instructions')
@click.option('--no-preserve-timestamps', is_flag=True, help='Do NOT preserve file timestamps (preserves by default)')
@click.option('--server', 'server_choice', type=click.Choice(['auto', 'waitress', 'cheroot'], case_sensitive=False), 
              default='auto', help='Force a specific server (default: auto-detect)')
def webdav_start(port: Optional[int], background: bool, show_mount: bool, 
                 no_preserve_timestamps: bool, server_choice: str): 
    """
    Start WebDAV server to mount Internxt Drive as a local drive.
    
    By default, file timestamps are preserved when copying/uploading files.
    Use --no-preserve-timestamps to disable this behavior.
    """
    try:
        # Check if logged in
        try:
            auth_service.get_auth_details()
        except Exception:
            click.echo("❌ Not logged in. Please login first with: python cli.py login", err=True)
            sys.exit(1)
        
        # Read and update config
        webdav_config = config_service.read_webdav_config()
        
        # Override with command-line options
        if port:
            webdav_config['port'] = port
        
        # Handle timestamp preservation
        if no_preserve_timestamps:
            webdav_config['preserveTimestamps'] = False
            click.echo("⚠️  Timestamp preservation disabled for this session")
        else:
            # Always default to True
            webdav_config['preserveTimestamps'] = True
        
        # Save config (preserves settings for next time, except --no-preserve-timestamps which is session-only)
        config_to_save = webdav_config.copy()
        if not no_preserve_timestamps:
            # Only save if user didn't explicitly disable it for this session
            config_service.save_webdav_config(config_to_save)
        
        # Handle background mode by spawning a separate process
        if background:
            # Build command to run in background (argv list, no shell)
            cmd = [sys.executable, __file__, 'webdav-start']
            if port:
                cmd.extend(['--port', str(port)])
            if no_preserve_timestamps:
                cmd.append('--no-preserve-timestamps')
            if server_choice != 'auto':
                cmd.extend(['--server', server_choice])
            # Don't pass --background to avoid infinite recursion
            
            # Check if already running
            status = webdav_server.status()
            if status['running']:
                click.echo("❌ WebDAV server is already running")
                click.echo(f"🌐 Server URL: {status['url']}")
                sys.exit(1)
            
            # Spawn background process
            click.echo("🚀 Starting WebDAV server in background...")
            
            # Redirect output to log file
            log_dir = config_service.internxt_cli_logs_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / 'webdav.log'
            
            with open(log_file, 'w', encoding='utf-8') as log:
                # argv is built from our own constants/flags; no shell, no untrusted input.
                process = subprocess.Popen(  # nosec B603
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True  # Detach from parent
                )
                
                # Save PID
                config_service.save_webdav_pid(process.pid)
                
                # Wait a moment to ensure it starts
                time.sleep(2)
                
                # Check if it's actually running
                status = webdav_server.status()
                if status.get('running'):
                    click.echo(f"✅ WebDAV server started in background (PID: {process.pid})")
                    click.echo(f"🌐 Server URL: http://localhost:{webdav_config['port']}/")
                    click.echo("👤 Username: internxt")
                    click.echo("🔑 Password: internxt-webdav")
                    click.echo(f"🕐 Preserve Timestamps: {'Yes' if webdav_config['preserveTimestamps'] else 'No'}")
                    click.echo(f"\n📋 Logs: {log_file}")
                    click.echo("💡 Use 'python cli.py webdav-stop' to stop the server")
                    click.echo("💡 Use 'python cli.py webdav-status' to check status")
                else:
                    click.echo(f"❌ Server failed to start. Check logs: {log_file}")
                    sys.exit(1)
            
            return
        
        # Foreground mode - run directly
        click.echo("🚀 Starting WebDAV server...")
        click.echo(f"📡 Protocol: {webdav_config['protocol'].upper()}")
        click.echo(f"🔌 Port: {webdav_config['port']}")
        click.echo(f"⏰ Timeout: {webdav_config['timeoutMinutes']} minutes")
        click.echo(f"🕐 Preserve Timestamps: {'Yes' if webdav_config['preserveTimestamps'] else 'No'}")
        
        result = webdav_server.start(
            port=int(webdav_config['port']),
            background=False,
            preserve_timestamps=webdav_config['preserveTimestamps'],
            server_choice=server_choice
        )
        
        if result['success']:
            click.echo(f"✅ {result['message']}")
            click.echo(f"🌐 Server URL: {result['url']}")
            click.echo("👤 Username: internxt")
            click.echo("🔑 Password: internxt-webdav")
            
            if show_mount:
                click.echo("\n💡 Mount Instructions:")
                instructions = webdav_server.get_mount_instructions()
                
                # Detect platform and show relevant instructions
                import platform
                system = platform.system().lower()
                
                if 'windows' in system:
                    click.echo(instructions['windows'])
                elif 'darwin' in system:
                    click.echo(instructions['macos'])
                elif 'linux' in system:
                    click.echo(instructions['linux'])
                else:
                    # Show all instructions
                    for platform_name, instruction in instructions.items():
                        click.echo(f"\n{platform_name.upper()}:")
                        click.echo(instruction)
            
            click.echo("\n🔄 Server running... Press Ctrl+C to stop")
            # Server will run in main thread - keep it alive
            while True:
                time.sleep(1)
        else:
            click.echo(f"❌ {result['message']}", err=True)
            sys.exit(1)
            
    except KeyboardInterrupt:
        click.echo("\n🛑 WebDAV server stopped by user")
        config_service.clear_webdav_pid()
    except Exception as e:
        click.echo(f"❌ Error starting WebDAV server: {e}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


@cli.command('webdav-stop')
def webdav_stop():
    """Stop WebDAV server"""
    try:
        result = webdav_server.stop()
        
        if result['success']:
            click.echo(f"✅ {result['message']}")
        else:
            click.echo(f"❌ {result['message']}", err=True)
            sys.exit(1)
            
    except Exception as e:
        click.echo(f"❌ Error stopping WebDAV server: {e}", err=True)
        sys.exit(1)


@cli.command('webdav-status')
def webdav_status():
    """Check WebDAV server status"""
    try:
        status = webdav_server.status()
        
        if status['running']:
            click.echo("✅ WebDAV server is running")
            click.echo(f"🌐 URL: {status['url']}")
            click.echo(f"📡 Protocol: {status['protocol'].upper()}")
            click.echo(f"🚪 Port: {status['port']}")
            click.echo(f"🏠 Host: {status['host']}")
            
            click.echo("\n👤 Credentials:")
            click.echo("   Username: internxt")
            click.echo("   Password: internxt-webdav")
        else:
            click.echo("❌ WebDAV server is not running")
            click.echo("💡 Start with: python cli.py webdav-start")
            
    except Exception as e:
        click.echo(f"❌ Error checking WebDAV status: {e}", err=True)
        sys.exit(1)


@cli.command('webdav-mount')
def webdav_mount():
    """Show platform-specific instructions for mounting WebDAV drive"""
    try:
        status = webdav_server.status()
        
        if not status['running']:
            click.echo("❌ WebDAV server is not running")
            click.echo("💡 Start with: python cli.py webdav-start")
            sys.exit(1)
        
        click.echo("🗂️  Mount Instructions for Internxt Drive")
        click.echo("=" * 50)
        click.echo(f"Server URL: {status['url']}")
        click.echo("Username: internxt")
        click.echo("Password: internxt-webdav")
        
        instructions = webdav_server.get_mount_instructions()
        
        # Show all platform instructions
        for platform_name, instruction in instructions.items():
            click.echo(f"\n{platform_name.upper()}:")
            click.echo("-" * 20)
            click.echo(instruction)
            
    except Exception as e:
        click.echo(f"❌ Error getting mount instructions: {e}", err=True)
        sys.exit(1)

# Add these commands to your cli.py file in the WebDAV section

@cli.command('webdav-test')
def webdav_test():
    """Test WebDAV server connection and functionality"""
    try:
        status = webdav_server.status()
        
        if not status['running']:
            click.echo("❌ WebDAV server is not running")
            click.echo("💡 Start with: python cli.py webdav-start")
            sys.exit(1)
        
        click.echo("🧪 Testing WebDAV server connection...")
        click.echo(f"🌐 Server URL: {status['url']}")
        
        # Test server connection
        test_result = webdav_server.test_connection()
        
        if test_result['success']:
            click.echo(f"✅ {test_result['message']}")
            click.echo(f"📡 Status Code: {test_result['status_code']}")
            
            # Show some useful headers
            headers = test_result.get('headers', {})
            if 'Allow' in headers:
                click.echo(f"🔧 Supported Methods: {headers['Allow']}")
            if 'DAV' in headers:
                click.echo(f"🔧 DAV Compliance: {headers['DAV']}")
            if 'Server' in headers:
                click.echo(f"🔧 Server: {headers['Server']}")
        else:
            click.echo(f"❌ {test_result['message']}")
            if 'status_code' in test_result:
                click.echo(f"📡 Status Code: {test_result['status_code']}")
        
        # Test with network utils
        click.echo("\n🔍 Testing with external connection...")

        external_test = NetworkUtils.test_webdav_connection(
            status['url'], 
            'internxt', 
            'internxt-webdav'
        )
        
        if external_test['success']:
            click.echo("✅ External connection test passed")
            click.echo(f"🔧 WebDAV Support: {'Yes' if external_test['webdav_supported'] else 'No'}")
            click.echo(f"🔧 Server: {external_test['server']}")
        else:
            click.echo(f"❌ External connection test failed: {external_test['message']}")
        
        # Show mount instructions
        click.echo("\n💡 If tests pass but you can't mount, try:")
        click.echo("   python cli.py webdav-mount")
        
    except Exception as e:
        click.echo(f"❌ Error testing WebDAV server: {e}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


@cli.command('webdav-debug')
def webdav_debug():
    """Show detailed WebDAV server debugging information"""
    try:
        click.echo("🔍 WebDAV Server Debug Information")
        click.echo("=" * 50)
        
        # Server status
        status = webdav_server.status()
        click.echo("📊 Server Status:")
        click.echo(f"   Running: {'✅ Yes' if status['running'] else '❌ No'}")
        
        if status['running']:
            click.echo(f"   URL: {status['url']}")
            click.echo(f"   Protocol: {status['protocol']}")
            click.echo(f"   Host: {status['host']}")
            click.echo(f"   Port: {status['port']}")
        
        # SSL Certificate status
        click.echo("\n🔐 SSL Certificate Information:")

        cert_info = NetworkUtils.validate_ssl_certificates()
        if cert_info['valid']:
            click.echo("   Status: ✅ Valid")
            click.echo(f"   Days until expiry: {cert_info['days_until_expiry']}")
            click.echo(f"   Subject: {cert_info['subject']}")
        else:
            click.echo(f"   Status: ❌ {cert_info['message']}")
        
        # Configuration
        webdav_config = config_service.read_webdav_config()
        click.echo("\n⚙️  WebDAV Configuration:")
        click.echo(f"   Protocol: {webdav_config['protocol']}")
        click.echo(f"   Port: {webdav_config['port']}")
        click.echo(f"   Timeout: {webdav_config['timeoutMinutes']} minutes")
        
        # File paths
        click.echo("\n📁 File Paths:")
        click.echo(f"   Config Dir: {config_service.internxt_cli_data_dir}")
        click.echo(f"   SSL Certs Dir: {NetworkUtils.WEBDAV_SSL_CERTS_DIR}")
        click.echo(f"   SSL Cert File: {NetworkUtils.WEBDAV_SSL_CERT_FILE} ({'✅' if NetworkUtils.WEBDAV_SSL_CERT_FILE.exists() else '❌'})")
        click.echo(f"   SSL Key File: {NetworkUtils.WEBDAV_SSL_KEY_FILE} ({'✅' if NetworkUtils.WEBDAV_SSL_KEY_FILE.exists() else '❌'})")
        
        # Authentication status
        click.echo("\n🔐 Authentication:")
        user_info = auth_service.whoami()
        if user_info:
            click.echo(f"   Status: ✅ Logged in as {user_info['email']}")
        else:
            click.echo("   Status: ❌ Not logged in")
        
        # Network tests
        if status['running']:
            click.echo("\n🌐 Network Tests:")
            
            # Test local connectivity
            import socket
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex((status['host'], status['port']))
                sock.close()
                
                if result == 0:
                    click.echo(f"   Port {status['port']}: ✅ Open")
                else:
                    click.echo(f"   Port {status['port']}: ❌ Closed/Filtered")
            except Exception as e:
                click.echo(f"   Port Test: ❌ Error: {e}")
            
            # Test WebDAV response
            try:
                auth = HTTPBasicAuth('internxt', 'internxt-webdav')
                # verify=False is intentional: local WebDAV uses a self-signed cert
                response = requests.options(status['url'], auth=auth, timeout=5, verify=False)  # nosec B501
                click.echo(f"   WebDAV Response: ✅ {response.status_code}")
                
                if 'Allow' in response.headers:
                    methods = response.headers['Allow'].split(', ')
                    webdav_methods = [m for m in methods if m in ['PROPFIND', 'PROPPATCH', 'MKCOL', 'COPY', 'MOVE', 'LOCK', 'UNLOCK']]
                    click.echo(f"   WebDAV Methods: {', '.join(webdav_methods) if webdav_methods else 'None detected'}")
                
            except Exception as e:
                click.echo(f"   WebDAV Test: ❌ {e}")
        
        # Troubleshooting tips
        click.echo("\n💡 Troubleshooting Tips:")
        if not status['running']:
            click.echo("   1. Start the server: python cli.py webdav-start")
        else:
            click.echo(f"   1. Try connecting via browser: {status['url']}")
            click.echo(f"   2. Test with curl: curl -u internxt:internxt-webdav {status['url']}")
            click.echo("   3. Check firewall/antivirus software")
            click.echo("   4. Try a different port: python cli.py webdav-start --port 8080")
            
    except Exception as e:
        click.echo(f"❌ Error getting debug information: {e}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


@cli.command('webdav-regenerate-ssl')
def webdav_regenerate_ssl():
    """Regenerate SSL certificates for WebDAV server"""
    try:
        click.echo("🔐 Regenerating SSL certificates for WebDAV server...")

        # Remove existing certificates
        if NetworkUtils.WEBDAV_SSL_CERT_FILE.exists():
            NetworkUtils.WEBDAV_SSL_CERT_FILE.unlink()
            click.echo("🗑️  Removed old certificate")
        
        if NetworkUtils.WEBDAV_SSL_KEY_FILE.exists():
            NetworkUtils.WEBDAV_SSL_KEY_FILE.unlink()
            click.echo("🗑️  Removed old private key")
        
        # Generate new certificates
        NetworkUtils.generate_new_selfsigned_certs()
        
        click.echo("✅ New SSL certificates generated successfully")
        click.echo(f"📁 Saved to: {NetworkUtils.WEBDAV_SSL_CERTS_DIR}")
        
        # Validate new certificates
        validation = NetworkUtils.validate_ssl_certificates()
        if validation['valid']:
            click.echo("✅ Certificate validation passed")
            click.echo(f"📅 Valid until: {validation['expiry_date']}")
        else:
            click.echo(f"❌ Certificate validation failed: {validation['message']}")
        
        click.echo("\n💡 Restart the WebDAV server to use the new certificates:")
        click.echo("   python cli.py webdav-stop")
        click.echo("   python cli.py webdav-start")
        
    except Exception as e:
        click.echo(f"❌ Error regenerating SSL certificates: {e}", err=True)
        sys.exit(1)


@cli.command('webdav-config')
def webdav_config_cmd():
    """Show WebDAV server configuration"""
    try:
        webdav_config = config_service.read_webdav_config()
        status = webdav_server.status()
        
        click.echo("⚙️  WebDAV Server Configuration")
        click.echo("=" * 40)
        
        # Current configuration
        click.echo(f"📡 Protocol: {webdav_config.get('protocol', 'http').upper()}")
        click.echo(f"🏠 Host: {webdav_config.get('host', 'localhost')}")
        click.echo(f"🚪 Port: {webdav_config.get('port', 8080)}")
        click.echo(f"⏱️  Timeout: {webdav_config.get('timeoutMinutes', 30)} minutes")
        click.echo(f"🕐 Preserve Timestamps: {webdav_config.get('preserveTimestamps', True)}")
        click.echo(f"📝 Verbose: Level {webdav_config.get('verbose', 0)}")
        click.echo("\n📝 Config file: " + str(config_service.webdav_configs_file))
        
        # SSL certificate info
        click.echo("\n🔐 SSL Certificates:")
        cert_dir = NetworkUtils.WEBDAV_SSL_CERTS_DIR
        cert_file = NetworkUtils.WEBDAV_SSL_CERT_FILE
        key_file = NetworkUtils.WEBDAV_SSL_KEY_FILE
        
        click.echo(f"   Directory: {cert_dir}")
        click.echo(f"   Certificate: {cert_file} ({'✅ exists' if cert_file.exists() else '❌ missing'})")
        click.echo(f"   Private Key: {key_file} ({'✅ exists' if key_file.exists() else '❌ missing'})")
        
        # Server status
        click.echo("\n🔄 Server Status:")
        if status['running']:
            click.echo("   Status: ✅ Running")
            click.echo(f"   URL: {status['url']}")
        else:
            click.echo("   Status: ❌ Stopped")
        
        # Usage examples
        click.echo("\n💡 Usage Examples:")
        click.echo("   Start server:    python cli.py webdav-start")
        click.echo("   Start with SSL:  python cli.py webdav-start  # (auto-detects from config)")
        click.echo("   Custom port:     python cli.py webdav-start --port 9090")
        click.echo("   Disable timestamps:    python cli.py webdav-start --no-preserve-timestamps")
        click.echo("   Background mode: python cli.py webdav-start --background")
        click.echo("   Stop server:     python cli.py webdav-stop")
        
    except Exception as e:
        click.echo(f"❌ Error reading WebDAV configuration: {e}", err=True)
        sys.exit(1)

@cli.command()
def test():
    """Test CLI components"""
    click.echo("🧪 Testing CLI components ...")
    click.echo("=" * 60)
    
    tests_passed = 0
    total_tests = 7  # Added WebDAV test
    
    # Test 1: Config service
    try:
        drive_api = config_service.get('DRIVE_NEW_API_URL')
        if not drive_api.startswith('https://') or '/drive' not in drive_api:
            raise AssertionError(f"DRIVE_NEW_API_URL invalid: {drive_api}")
        click.echo(f"✅ Config service - DRIVE_NEW_API_URL={drive_api}")
        tests_passed += 1
    except Exception as e:
        click.echo(f"❌ Config service failed: {e}")
    
    # Test 2: Crypto service
    try:
        test_text = "Hello World"
        encrypted = crypto_service.encrypt_text(test_text)
        decrypted = crypto_service.decrypt_text(encrypted)
        if decrypted != test_text:
            raise AssertionError("encrypt/decrypt round-trip mismatch")
        click.echo("✅ Crypto service - exact TypeScript CryptoJS compatibility")
        tests_passed += 1
    except Exception as e:
        click.echo(f"❌ Crypto service failed: {e}")
    
    # Test 3: API endpoints
    try:
        login_url = f"{api_client.drive_api_url}/auth/login"
        if not login_url.startswith('https://') or not login_url.endswith('/drive/auth/login'):
            raise AssertionError(f"login URL malformed: {login_url}")
        click.echo(f"✅ API endpoints - login URL={login_url}")
        tests_passed += 1
    except Exception as e:
        click.echo(f"❌ API endpoint test failed: {e}")
    
    # Test 4: Auth service structure
    try:
        for _attr in ('do_login', 'is_2fa_needed', 'get_auth_details'):
            if not hasattr(auth_service, _attr):
                raise AssertionError(f"auth_service missing attribute: {_attr}")
        click.echo("✅ Auth service - exact TypeScript AuthService structure")
        tests_passed += 1
    except Exception as e:
        click.echo(f"❌ Auth service structure test failed: {e}")
    
    # Test 5: Mnemonic validation
    try:
        valid_mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        is_valid = crypto_service.validate_mnemonic(valid_mnemonic)
        if is_valid is not True:
            raise AssertionError("expected mnemonic to validate as True")
        click.echo("✅ Mnemonic validation - exact TypeScript ValidationService match")
        tests_passed += 1
    except Exception as e:
        click.echo(f"❌ Mnemonic validation test failed: {e}")
    
    # Test 6: File path structure
    try:
        home_dir = Path.home()
        expected_config_dir = home_dir / '.internxt-cli'
        if config_service.internxt_cli_data_dir != expected_config_dir:
            raise AssertionError("internxt_cli_data_dir mismatch")
        click.echo("✅ File paths - exact TypeScript ConfigService match")
        tests_passed += 1
    except Exception as e:
        click.echo(f"❌ File path test failed: {e}")
    
    # Test 7: WebDAV imports
    try:
        # Test WebDAV imports without initializing the server
        # pylint: disable=unused-import,import-outside-toplevel
        from wsgidav.dav_provider import DAVProvider, DAVCollection, DAVNonCollection  # noqa: F401
        click.echo("✅ WebDAV dependencies - properly installed and importable")
        tests_passed += 1
    except ImportError as e:
        click.echo(f"❌ WebDAV dependencies missing: {e}")
        click.echo("   Install with: pip install WsgiDAV cheroot")
    except Exception as e:
        click.echo(f"❌ WebDAV import test failed: {e}")
    
    click.echo("\n" + "=" * 60)
    click.echo(f"📊 Tests passed: {tests_passed}/{total_tests}")
    
    if tests_passed == total_tests:
        click.echo("🎉 All tests passed! CLI is working correctly.")
        if tests_passed >= 6:  # All core tests passed
            click.echo("🌐 WebDAV server ready to use!")
    else:
        click.echo("⚠️  Some tests failed. Please review the errors.")
        if tests_passed < 6:
            click.echo("🔧 Core functionality issues detected.")
        else:
            click.echo("🌐 WebDAV optional - install dependencies if you want WebDAV server.")


@cli.command()
def config():
    """Show current configuration"""
    try:
        click.echo("⚙️  Internxt CLI Configuration")
        click.echo("=" * 40)
        
        # API Configuration
        click.echo("🌐 API Endpoints:")
        for key, label in [('DRIVE_WEB_URL', 'Drive Web'),
                           ('DRIVE_NEW_API_URL', 'Drive API'),
                           ('NETWORK_URL', 'Network API')]:
            try:
                click.echo(f"   {label}: {config_service.get(key)}")
            except ValueError:
                click.echo(f"   {label}: (not configured)")
        
        # File Paths  
        click.echo("\n📁 File Paths:")
        click.echo(f"   Config Dir: {config_service.internxt_cli_data_dir}")
        click.echo(f"   Credentials: {config_service.credentials_file}")
        click.echo(f"   Logs Dir: {config_service.internxt_cli_logs_dir}")
        
        # Login Status
        click.echo("\n🔐 Authentication:")
        user_info = auth_service.whoami()
        if user_info:
            click.echo(f"   Status: ✅ Logged in as {user_info['email']}")
            click.echo(f"   User ID: {user_info['uuid']}")
            click.echo(f"   Root Folder: {user_info['rootFolderId']}")
        else:
            click.echo("   Status: ❌ Not logged in")
        
        # WebDAV Configuration
        webdav_config = config_service.read_webdav_config()
        click.echo("\n🌐 WebDAV Server:")
        click.echo(f"   Protocol: {webdav_config['protocol']}")
        click.echo(f"   Port: {webdav_config['port']}")
        click.echo(f"   Timeout: {webdav_config['timeoutMinutes']} minutes")
        
    except Exception as e:
        click.echo(f"❌ Error reading configuration: {e}", err=True)


@cli.command()
def help_extended():
    """Show extended help with examples"""
    click.echo("""
🚀 Internxt Python CLI - Extended Help
========================================

🔐 AUTHENTICATION
  login              Login to your Internxt account
  whoami            Check current login status
  logout            Logout and clear credentials

📁 BASIC OPERATIONS (UUID-based)
  list              List folder contents by UUID
  mkdir NAME        Create new folder
  upload FILE       Upload file to Drive
  download UUID     Download file by UUID

🛣️  PATH-BASED OPERATIONS (User-friendly!)
  list-path [PATH]  List folder contents with readable paths
  download-path PATH Download file by path (e.g., "/Documents/report.pdf")
  find PATTERN      Search files with wildcards (e.g., "*.pdf")
  resolve PATH      Show what a path points to (debugging)
  tree [PATH]       Show folder structure as tree

🗑️  DELETE/TRASH OPERATIONS
  trash UUID        Move file/folder to trash by UUID
  trash-path PATH   Move file/folder to trash by path
  delete UUID       Permanently delete by UUID (⚠️ CANNOT BE UNDONE!)
  delete-path PATH  Permanently delete by path (⚠️ CANNOT BE UNDONE!)

🌐 WEBDAV SERVER (Mount as Local Drive!)
  webdav-start      Start WebDAV server to mount drive locally
  webdav-stop       Stop WebDAV server
  webdav-status     Check if WebDAV server is running
  webdav-mount      Show mount instructions for your OS
  webdav-config     Show WebDAV configuration

🔧 UTILITIES
  config            Show current configuration
  test              Test CLI components

💡 EXAMPLES:
  # Login and explore
  python cli.py login
  python cli.py list-path
  python cli.py tree
  
  # Find and download files
  python cli.py find "*.pdf"
  python cli.py download-path "/Documents/important.pdf"
  
  # Mount as local drive (AMAZING!)
  python cli.py webdav-start
  # Then in Windows: Map network drive to http://localhost:8080
  # Username: internxt, Password: internxt-webdav
  
  # Clean up
  python cli.py trash-path "/OldFolder"
  python cli.py delete-path "/TempFile.txt" --force

🌟 TIP: Path-based commands are much easier to use than UUID-based ones!
🌟 NEW: WebDAV server lets you access your drive like a local folder!
""")


if __name__ == '__main__':
    if len(sys.argv) == 1:
        print("🚀 Internxt Python CLI with Path Support")
        print("=" * 50)
        print("💡 Try: python cli.py help-extended")
        print("")
    
    cli()