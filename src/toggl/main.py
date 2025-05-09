#!/usr/bin/env python3
import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import total_ordering
from pathlib import Path
from typing import Any, Self, Literal

import requests
from simple_term_menu import TerminalMenu

# Script directory for state files
DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_FILE = DATA_DIR / "toggl_config.json"
STATE_FILE = DATA_DIR / "toggl_state.json"

# Toggl API v9 base URL
TOGGL_API_BASE_URL = "https://api.track.toggl.com/api/v9"

DEFAULT_CLIENT = "Lunatech"  # Default client name, should come last when sorting


# --- Data Classes ---


@dataclass
@total_ordering
class Project:
    id: int
    name: str
    workspace_id: int
    client: str | None = None
    alias: str | None = None  # Custom alias added by the script
    billable: bool = True  # Default is billable

    def _get_sort_key(self) -> tuple:
        """
        Generates a sort key for the project based on client and name.
        The key respects the custom order: Regular Clients < DEFAULT_CLIENT < None.
        """
        if self.client is None:
            client_rank = 2
        elif self.client == DEFAULT_CLIENT:
            client_rank = 1
        else:
            client_rank = 0

        return client_rank, self.client, self.name

    def __lt__(self, other: Self) -> bool:
        """
        Define custom ordering for projects:
        1. First by client (lexicographically), but DEFAULT_CLIENT comes last and None at the very end
        2. For the same client, order by name lexicographically
        """
        return self._get_sort_key() < other._get_sort_key()

    def __str__(self) -> str:
        """
        Returns a string representation of the project including name, client, and alias if they exist.
        """
        result = ""
        if self.alias:
            result += f"[{self.alias}] "
        result += self.name
        if self.client and self.client not in self.name:
            result += f" — {self.client}"
        if self.billable:
            result += " (€)"
        return result


@dataclass
class Config:
    # Store projects indexed by their ID for easier merging/lookup
    projects: dict[int, Project] = field(default_factory=dict)
    default_project_id: int | None = None

    def get_project(self, selector: str) -> Project | None:
        """Get a project by alias or name."""
        if not selector:
            default_project = self._get_default_project()
            if default_project:
                print(f"Using default project '{default_project.name}'")
            else:
                print("Error: No default project set.", file=sys.stderr)
            return default_project

        for project in self.projects.values():
            alias_matches = project.alias and project.alias == selector
            name_matches = project.name.lower() == selector.lower()
            if alias_matches or name_matches:
                return project
        return None

    def _get_default_project(self) -> Project | None:
        return self.projects.get(self.default_project_id) if self.default_project_id else None


# --- Helper Functions ---


def _get_api_token() -> str:
    """Retrieves the Toggl API token from the environment variable."""
    token = os.environ.get("TOGGL_API_TOKEN")
    if not token:
        print("Error: TOGGL_API_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)
    return token


def _make_request(method: str, endpoint: str, data: dict[str, Any] | None = None) -> Any:
    """Makes an authenticated request to the Toggl API."""
    token = _get_api_token()
    url = f"{TOGGL_API_BASE_URL}{endpoint}"
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.request(
            method, url, auth=(token, "api_token"), headers=headers, json=data
        )
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        # Handle potential empty responses for certain actions (like stopping a timer)
        if response.status_code == 200 and response.text:
            # Check if response content type is JSON before attempting to parse
            if "application/json" in response.headers.get("Content-Type", ""):
                return response.json()
            else:
                # Handle non-JSON 200 responses if necessary, or return None/True
                # print(f"Warning: Received non-JSON response for {method} {endpoint}", file=sys.stderr)
                return None  # Or return True to indicate success
        elif response.status_code == 204:  # No Content
            return None  # Success, but no body
        else:
            return response.text  # Return text for non-JSON success if needed, like stopping timers
    except requests.exceptions.RequestException as e:
        print(f"API Request Error ({method} {url}): {e}", file=sys.stderr)
        if hasattr(e, "response") and e.response is not None:
            try:
                error_details = e.response.json()
                print(f"API Error Details: {error_details}", file=sys.stderr)
            except json.JSONDecodeError:
                print(f"API Error Content: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error decoding JSON response from {method} {url}", file=sys.stderr)
        sys.exit(1)


def _load_json(filepath: Path) -> Any:
    """Loads JSON data from a file, returning None in case of error."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        print(
            f"Error: Could not decode JSON from {filepath}.",
            file=sys.stderr,
        )
        return None
    except IOError as e:
        print(f"Error reading file {filepath}: {e}", file=sys.stderr)
        return None


def _save_json(filepath: Path, data: Any) -> None:
    """Saves data to a JSON file."""
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=4)
    except IOError as e:
        print(f"Error writing file {filepath}: {e}", file=sys.stderr)
        sys.exit(1)


def _ensure_data_dir() -> None:
    """Ensures the data directory exists."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Error creating data directory {DATA_DIR}: {e}", file=sys.stderr)
        sys.exit(1)


def _load_config() -> Config:
    """Loads the configuration from the JSON file."""
    data = _load_json(CONFIG_FILE)
    if data is None:
        print("Error: Setup incomplete. Please run `toggl setup` first.", file=sys.stderr)
        sys.exit(1)

    # Reconstruct Project objects within the projects dictionary
    projects_data = data.get("projects", {})
    reconstructed_projects = {int(pid): Project(**pdata) for pid, pdata in projects_data.items()}
    data["projects"] = reconstructed_projects
    return Config(**data)


def _save_config(config: Config) -> None:
    """Saves the configuration to the JSON file."""
    _ensure_data_dir()
    _save_json(CONFIG_FILE, asdict(config))


def _get_current_utc_time() -> datetime:
    """Gets the current time in UTC."""
    return datetime.now(timezone.utc)


def _round_time_down(dt: datetime) -> datetime:
    """Rounds a datetime object down to the nearest 15 minutes."""
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _round_time_up(dt: datetime) -> datetime:
    """Rounds a datetime object up to the nearest 15 minutes."""
    if dt.minute % 15 == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt  # Already on a 15-min boundary
    return _round_time_down(dt) + timedelta(minutes=15)


def _format_iso(dt: datetime) -> str:
    """Formats a datetime object into ISO 8601 format for the API."""
    return dt.isoformat(timespec="seconds")


# --- Command Handlers ---


def _fetch_projects() -> list[Project]:
    """Fetches projects from the Toggl API."""
    # 1. Get User data including related data (organizations, workspaces, projects, clients)
    print("Fetching user data (organizations, workspaces, projects, clients)...")
    me_data = _make_request("GET", "/me?with_related_data=true")

    if not me_data:
        print("Error: Could not fetch data from /me endpoint.", file=sys.stderr)
        sys.exit(1)

    # 2. Process Clients
    clients_map: dict[int, str] = {}
    if clients_data := me_data.get("clients"):
        clients_map = {c["id"]: c["name"] for c in clients_data if "id" in c and "name" in c}
        print(f"Processed {len(clients_map)} clients.")
    else:
        print("No clients data found in /me response or clients list is null.")

    # 3. Process Projects
    if projects_data := me_data.get("projects"):
        projects = [
            Project(
                id=p["id"],
                name=p["name"],
                workspace_id=p["workspace_id"],
                client=clients_map.get(p["client_id"]),
                # alias will be None initially
                billable=p["billable"],
            )
            for p in projects_data
            if p["active"] and not p["is_private"]
        ]
        print(f"Fetched {len(projects)} projects for the selected workspace.")
        projects.sort()  # Sort projects using the custom ordering defined in Project class
        return projects
    else:
        print("No projects data found in /me response or projects list is null.")
        return []  # Ensure it's an empty dict if no projects


def _projects_to_dict(projects: list[Project]) -> dict[int, Project]:
    """Converts a list of Project objects to a dictionary indexed by project ID."""
    return {project.id: project for project in projects}


def handle_init() -> None:
    """Fetches user, organization, workspace, clients, and projects, saving initial config."""
    print("Running initial setup using /me endpoint...")
    config = Config(projects=_projects_to_dict(_fetch_projects()))

    _save_config(config)
    print("Setup complete. Configuration updated from /me endpoint and saved to toggl_config.json")


@dataclass
class ProjectMenu:
    projects: list[Project]
    default_project_id: int | None = None

    def _shortcut_for_index(self, index: int) -> str:
        """Converts a numeric index to display character (0-9, a-p, r-z)."""

        def shortcut_from_char(char: str) -> str:
            return f"[{char}] " if char else ""

        if index < 10:
            return shortcut_from_char(str(index))  # 0-9 remain as numbers
        index = index - 10 + ord("a")
        if index >= ord("q"):
            index += 1  # Skip 'q' which is reserved for quitting
        if index < ord("z"):
            return shortcut_from_char(chr(index))
        return ""  # No shortcut for very large indices

    def _project_menu_str(self, project_index: int) -> str:
        """Generates a string for the project menu item."""
        project = self.projects[project_index]

        project_str = self._shortcut_for_index(project_index)
        if project.id == self.default_project_id:
            project_str += "[DEFAULT] "
        project_str += str(project)
        return project_str

    def _make_config(self) -> Config:
        """Creates a Config object from the current project list and default project ID."""
        return Config(
            projects=_projects_to_dict(self.projects),
            default_project_id=self.default_project_id,
        )

    def _is_alias_used(self, alias: str) -> bool:
        """Checks if the given alias is already used by a different project."""
        return any(project.alias == alias for project in self.projects)

    def _show_change_alias_menu(self, selected_project: Project) -> None:
        """Displays the alias change menu and handles user input."""
        new_alias_val = input(
            f"Enter new alias for '{selected_project}' (blank to cancel): "
        ).strip()

        if not new_alias_val or new_alias_val == selected_project.alias:
            print("Alias not changed.")
            return

        if self._is_alias_used(new_alias_val):
            print(
                f"Error: Alias '{new_alias_val}' is already in use by another project. Alias not changed.",
                file=sys.stderr,
            )
            return

        selected_project.alias = new_alias_val

    def _show_edit_project_menu(self, selected_project_idx: int) -> Literal["a", "r", "d", "b"]:
        """Displays the project editing menu and returns the selected action."""
        selected_project = self.projects[selected_project_idx]
        project_edit_items = ["[a] Change Alias"]

        if selected_project.alias:
            project_edit_items.append("[r] Remove Alias")

        is_currently_default = selected_project.id == self.default_project_id
        if not is_currently_default:
            project_edit_items.append("[d] Set as default project")

        project_edit_items.append("[b] Back to project list")

        terminal_menu_actions = TerminalMenu(
            menu_entries=project_edit_items,
            title=self._project_menu_str(selected_project_idx),
            cycle_cursor=True,
            clear_screen=True,
            shortcut_key_highlight_style=("fg_yellow", "bold"),
            shortcut_brackets_highlight_style=("fg_yellow",),
        )
        selected_action_idx = terminal_menu_actions.show()

        if selected_action_idx is None:
            return "b"  # Esc on action menu, goes back to project list
        return project_edit_items[selected_action_idx][1]  # Get the action character

    def _edit_project_menu_loop(self, selected_project_idx: int) -> None:
        """Handles the project editing menu loop."""
        selected_project = self.projects[selected_project_idx]

        choice = self._show_edit_project_menu(selected_project_idx)

        if choice == "a":  # Change Alias
            self._show_change_alias_menu(selected_project)
        elif choice == "r":  # Remove Alias
            selected_project.alias = None
            print(f"Alias removed for '{selected_project}'.")
        elif choice == "d":  # Set as Default
            self.default_project_id = selected_project.id
            print(f"'{selected_project.name}' is now the default project.")

    def _show_select_project_menu(self) -> int | Literal["q"] | None:
        """Displays the project selection menu and returns either
        - the index of the selected project
        - "q" if the user chooses to save and quit
        - None if the user presses Esc to exit without saving"""
        select_project_items: list[str | None] = [
            self._project_menu_str(idx) for idx in range(len(self.projects))
        ]

        select_project_items.append(None)
        select_project_items.append("[q] Save and quit")

        terminal_menu_projects = TerminalMenu(
            menu_entries=select_project_items,
            title="Select a Project (Press Esc to quit without saving)",
            cycle_cursor=True,
            clear_screen=True,
            shortcut_key_highlight_style=("fg_green", "bold"),
            shortcut_brackets_highlight_style=("fg_green",),
        )
        selected_project_index = terminal_menu_projects.show()

        if selected_project_index == len(select_project_items) - 1:
            # User selected the quit option
            return "q"
        return selected_project_index

    def select_project_menu_loop(self) -> None:
        while True:
            choice = self._show_select_project_menu()

            if choice == "q":
                _save_config(self._make_config())
                print("Configuration saved.")
                break
            elif choice is None:
                print("Quit without saving.")
                break

            # User selected a project index
            self._edit_project_menu_loop(choice)


def handle_projects() -> None:
    """Interactively manage project aliases and default project."""
    old_config: Config = _load_config()
    new_projects: list[Project] = _fetch_projects()

    # Merge API projects with existing config projects/aliases
    for new_project in new_projects:
        old_project = old_config.projects.get(new_project.id)
        if old_project:
            new_project.alias = old_project.alias  # Keep existing alias

    # Check if default project still exists
    default_project_still_exists = old_config.default_project_id and any(
        p.id == old_config.default_project_id for p in new_projects
    )
    new_default_id = old_config.default_project_id if default_project_still_exists else None

    new_config = ProjectMenu(
        projects=new_projects,
        default_project_id=new_default_id,
    )

    new_config.select_project_menu_loop()


def handle_start(description: str, project: str, billable: bool) -> None:
    """Starts a new time entry."""

    config = _load_config()

    # Check if a task is already running
    current_entry = _make_request("GET", "/me/time_entries/current")
    if current_entry:
        print(
            f"Error: A task (ID: {current_entry.get('id')}) is already running.",
            file=sys.stderr,
        )
        print("Please end the current task first using `toggl end`.")
        sys.exit(1)

    # Determine Project ID
    project: Project = config.get_project(project)
    if not project:
        # No project specified and no default project set, raise error
        print(
            "Error: No project specified and no default project set. Please specify a project or set a default using `toggl projects`.",
            file=sys.stderr,
        )
        sys.exit(1)

    start_time = _get_current_utc_time()
    rounded_start_time = _round_time_down(start_time)
    start_time_iso = _format_iso(rounded_start_time)

    payload = {
        "description": description,
        "workspace_id": project.workspace_id,
        "project_id": project.id,  # Can be None
        "start": start_time_iso,
        "duration": -1,  # Indicates a running timer
        "created_with": "toggl-cli-script",
        "billable": billable and project.billable,
    }
    # Clean payload: remove None values if API doesn't like them (project_id)
    payload = {k: v for k, v in payload.items() if v is not None}

    start_time_str = rounded_start_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"Starting task '{description}' for project '{project.name}' at {start_time_str}...")

    try:
        new_entry = _make_request(
            "POST", f"/workspaces/{project.workspace_id}/time_entries", data=payload
        )
        if new_entry and "id" in new_entry:
            print(f"Task started successfully. ID: {new_entry['id']}")
        else:
            print(
                "Error: Failed to start task. API response did not contain expected data.",
                file=sys.stderr,
            )
            print(f"API Response: {new_entry}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error starting task: {e}", file=sys.stderr)
        sys.exit(1)


def handle_end() -> None:
    """Stops the currently running time entry with time rounding."""
    config = _load_config()

    # Get the current running time entry from the API
    current_entry = _make_request("GET", "/me/time_entries/current")

    if not current_entry:
        print(
            "Error: No task seems to be running according to the Toggl API.",
            file=sys.stderr,
        )
        sys.exit(1)

    task_id = current_entry["id"]
    workspace_id = current_entry["workspace_id"]
    start_time_str = current_entry["start"]

    print(f"Attempting to stop task ID: {task_id}...")

    try:
        # Parse the start time from the API response
        start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        start_time_rounded_down = _round_time_down(start_time)

        end_time_actual = _get_current_utc_time()
        end_time_rounded_down = _round_time_down(end_time_actual)

        # Apply special rounding rule: if start and end fall in the same 15-min block, round end *up*.
        if start_time_rounded_down == end_time_rounded_down:
            final_end_time = _round_time_up(end_time_actual)
            print(
                f"Note: Task started ({start_time_rounded_down.strftime('%H:%M')}) and ended ({end_time_actual.strftime('%H:%M')}) within the same 15min block. Rounding end time UP."
            )
        else:
            final_end_time = end_time_rounded_down
            print(f"Note: Rounding end time ({end_time_actual.strftime('%H:%M')}) DOWN.")

        final_end_time_iso = _format_iso(final_end_time)

        # Use the PATCH method to update the existing time entry's stop time
        payload = {
            "stop": final_end_time_iso,
        }
        print(
            f"Stopping task at calculated time: {final_end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )

        stopped_entry = _make_request(
            "PATCH",
            f"/workspaces/{workspace_id}/time_entries/{task_id}/stop",
            data=payload,
        )

        if stopped_entry:
            print(f"Task '{stopped_entry.get('description', 'N/A')}' stopped successfully.")
        else:
            # Sometimes PATCH returns 200 OK with the updated object, sometimes maybe just status?
            # Let's assume if no exception, it worked. Check response if needed.
            print("Task stop request sent. Assuming success (API response was minimal or empty).")

    except Exception as e:
        print(f"Error stopping task ID {task_id}: {e}", file=sys.stderr)
        sys.exit(1)


# --- Main Execution ---


def call_handler(args: argparse.Namespace) -> None:
    keys_to_ignore = ["handler", "command"]
    handler = args.handler
    handler_args = {k: v for k, v in vars(args).items() if k not in keys_to_ignore}
    handler(**handler_args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Toggl Command Line Assistant")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # Setup command
    parser_setup = subparsers.add_parser(
        "init",
        aliases=["i"],
        help="Initialize configuration (fetch org, workspace, projects)",
    )
    parser_setup.set_defaults(handler=handle_init)

    # Projects command
    parser_projects = subparsers.add_parser(
        "projects", aliases=["p"], help="List projects and manage aliases/defaults"
    )
    parser_projects.set_defaults(handler=handle_projects)

    # Start command
    parser_start = subparsers.add_parser("start", aliases=["s"], help="Start a new time entry")
    parser_start.add_argument("description", help="Description of the task")
    parser_start.add_argument(
        "-p",
        "--project",
        help="Project alias or name to assign the task to (uses default if not specified)",
    )
    parser_start.add_argument(
        "--no-billable",
        dest="billable",
        action="store_false",
        default=True,
        help="Mark the task as non-billable (default: billable)",
    )
    parser_start.set_defaults(handler=handle_start)

    # End command
    parser_end = subparsers.add_parser("end", aliases=["e"], help="End the current time entry")
    parser_end.set_defaults(handler=handle_end)

    args = parser.parse_args()
    call_handler(args)


if __name__ == "__main__":
    main()
