"""
Mark completed tasks as Ready to Invoice.

Usage:
    python mark_ready_to_invoice.py          # List completed tasks needing substatus
    python mark_ready_to_invoice.py --apply  # Actually update them
"""

import sys
from aroflo_connector import create_connector


def get_completed_tasks(connector):
    """Get completed tasks."""
    response = connector.request("tasks", {"where": "and|status|=|Completed"})
    zone_response = response.get("zoneresponse", response)
    return zone_response.get("tasks", [])


def main():
    apply_changes = "--apply" in sys.argv

    print("Mark Tasks Ready to Invoice")
    print("=" * 60)

    connector = create_connector()

    # Get Ready to Invoice substatus ID
    print("\n1. Finding 'Ready to Invoice' substatus...")
    substatus_id = connector.get_substatus_id("Ready to Invoice")
    if not substatus_id:
        print("   ERROR: Could not find 'Ready to Invoice' substatus")
        return

    print(f"   Found: {substatus_id}")

    # Get completed tasks
    print("\n2. Finding completed tasks...")
    tasks = get_completed_tasks(connector)

    if not tasks:
        print("   No completed tasks found")
        return

    # Filter to those not already marked
    tasks_to_update = []
    for task in tasks:
        substatus = task.get("substatus", {})
        current = substatus.get("substatus", "") if isinstance(substatus, dict) else ""

        if current != "Ready to Invoice":
            tasks_to_update.append({
                "taskid": task.get("taskid"),
                "taskno": task.get("taskno"),
                "taskname": task.get("taskname"),
                "current_substatus": current or "(none)",
            })

    print(f"   Found {len(tasks)} completed tasks")
    print(f"   {len(tasks_to_update)} need substatus update")

    if not tasks_to_update:
        print("\n   All completed tasks already marked Ready to Invoice!")
        return

    print("\n3. Tasks to update:")
    for t in tasks_to_update:
        print(f"   - {t['taskno'] or 'N/A'}: {t['taskname'][:50]} [{t['current_substatus']}]")

    if not apply_changes:
        print("\n" + "-" * 60)
        print("DRY RUN - No changes made")
        print("Run with --apply to update substatus")
        return

    # Apply changes
    print("\n4. Updating tasks...")
    success = 0
    failed = 0

    for t in tasks_to_update:
        try:
            connector.update_task_substatus(t["taskid"], substatus_id)
            print(f"   [OK] {t['taskname'][:40]}")
            success += 1
        except Exception as e:
            print(f"   [FAIL] {t['taskname'][:40]}: {e}")
            failed += 1

    print(f"\nDone: {success} updated, {failed} failed")


if __name__ == "__main__":
    main()
