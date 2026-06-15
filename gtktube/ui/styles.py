APP_CSS = """
.sidebar {
  background: alpha(currentColor, 0.04);
  border-right: 1px solid alpha(currentColor, 0.14);
}

.sidebar-list {
  background: transparent;
  padding: 8px 6px;
}

.nav-row {
  border-radius: 7px;
  margin: 1px 0;
  padding: 7px 8px;
}

.channel-nav-row {
  border-radius: 6px;
  margin: 0;
  padding: 5px 8px 5px 28px;
}

.channel-nav-label {
  font-size: 0.92em;
}

.channel-avatar {
  border-radius: 9999px;
  background: alpha(currentColor, 0.08);
}

.miniplayer {
  background: alpha(currentColor, 0.08);
  border-top: 1px solid alpha(currentColor, 0.18);
  padding: 8px 12px;
}

.queue-pane {
  background: alpha(currentColor, 0.02);
  border-left: 1px solid alpha(currentColor, 0.14);
}

.queue-row {
  padding: 6px;
  min-height: 86px;
}

.queue-row.skipped {
  opacity: 0.3;
}

.video-progress {
  background: alpha(currentColor, 0.2);
}

.video-progress progress {
  background-color: #f00;
  border: none;
  border-radius: 0;
  min-height: 3px;
}

.video-progress trough {
  background: transparent;
  border: none;
  border-radius: 0;
  min-height: 3px;
}

.timeline-overlay {
  background: transparent;
}

.video-tile {
  background: transparent;
  border: none;
  box-shadow: none;
  padding: 0;
}

.video-tile:hover {
  background: alpha(currentColor, 0.05);
}
"""
