-- Hermes Bridge game script descriptor. The game discovers *.gs.lua files on
-- its own; this file's resource name (hermes_bridge::/hermes_bridge/hermes_bridge.gs)
-- identifies the script state inside savegames, so never rename or move it.
function data()
	return {
		updateScript = { fileName = "hermes_bridge.script@update" },
		handleEventScript = { fileName = "hermes_bridge.script@handleEvent" },
		guiUpdateScript = { fileName = "hermes_bridge.script@guiUpdate" },
	}
end
