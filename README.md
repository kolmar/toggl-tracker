# Installation

```bash
# Make sure uv is installed
brew install uv
# Install the tool
uv tool install --from https://github.com/kolmar/toggl-tracker.git toggl-tracker
# Make sure the tool is available in the terminal
uv tool update-shell # Requires restarting terminal session
# Run
toggl -h
toggl projects
# Update the tool
uv tool upgrade toggl-tracker
```
