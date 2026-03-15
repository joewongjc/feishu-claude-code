#!/usr/bin/env python3
"""
Migrate session data from old format to new format.
Old: {user_id: {current, history}}
New: {user_id: {private: {current, history}, group_id: {...}}}
"""

import json
import os
import shutil
from datetime import datetime


def migrate_sessions(sessions_path: str):
    """Migrate sessions.json to new format"""

    # Check if file exists
    if not os.path.exists(sessions_path):
        print(f"❌ File not found: {sessions_path}")
        return False

    # Load old data
    print(f"📖 Loading {sessions_path}...")
    with open(sessions_path, 'r', encoding='utf-8') as f:
        old_data = json.load(f)

    print(f"✅ Loaded {len(old_data)} users")

    # Check if already migrated
    if old_data and isinstance(list(old_data.values())[0], dict):
        first_user_data = list(old_data.values())[0]
        if "private" in first_user_data or any(k.startswith("group_") or k.startswith("oc_") for k in first_user_data.keys() if k not in ["summaries"]):
            print("⚠️  Data appears to be already migrated (has 'private' or 'group_' keys)")
            response = input("Continue anyway? (y/N): ")
            if response.lower() != 'y':
                return False

    # Backup original file
    backup_path = f"{sessions_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"💾 Creating backup: {backup_path}")
    shutil.copy2(sessions_path, backup_path)

    # Migrate data
    new_data = {}
    for user_id, user_sessions in old_data.items():
        # Check if user_sessions has the old structure
        if "current" in user_sessions or "history" in user_sessions:
            # Old format: move everything to "private" key
            new_data[user_id] = {
                "private": {
                    "current": user_sessions.get("current", {}),
                    "history": user_sessions.get("history", []),
                }
            }
            # Preserve summaries at user level
            if "summaries" in user_sessions:
                new_data[user_id]["summaries"] = user_sessions["summaries"]
        else:
            # Already new format or empty
            new_data[user_id] = user_sessions

    # Validate migration
    print("🔍 Validating migration...")
    assert len(new_data) == len(old_data), "User count mismatch"

    for user_id in old_data:
        if "current" in old_data[user_id] or "history" in old_data[user_id]:
            assert "private" in new_data[user_id], f"Missing 'private' key for {user_id}"
            assert new_data[user_id]["private"]["current"] == old_data[user_id].get("current", {}), f"Current data mismatch for {user_id}"
            assert new_data[user_id]["private"]["history"] == old_data[user_id].get("history", []), f"History data mismatch for {user_id}"

    print("✅ Validation passed")

    # Write new data
    print(f"💾 Writing migrated data to {sessions_path}...")
    with open(sessions_path, 'w', encoding='utf-8') as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)

    print(f"✅ Migration complete!")
    print(f"📊 Migrated {len(new_data)} users")
    print(f"💾 Backup saved to: {backup_path}")
    print(f"\n⚠️  If you encounter issues, restore with:")
    print(f"   cp {backup_path} {sessions_path}")

    return True


if __name__ == "__main__":
    import sys

    # Default path
    default_path = os.path.expanduser("~/.feishu-claude/sessions.json")

    if len(sys.argv) > 1:
        sessions_path = sys.argv[1]
    else:
        sessions_path = default_path

    print("🚀 Session Data Migration Tool")
    print(f"📁 Target: {sessions_path}\n")

    success = migrate_sessions(sessions_path)

    if success:
        print("\n✅ Migration successful!")
        sys.exit(0)
    else:
        print("\n❌ Migration failed or cancelled")
        sys.exit(1)
