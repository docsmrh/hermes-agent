-- Hermes Bridge entry point: wires the modules together and exposes the
-- game-script callbacks referenced by hermes_bridge.gs.lua.
--
-- getOwningModId() must be called here, not from a helper file.

local MOD = (getOwningModId and getOwningModId()) or "hermes_bridge"

local function load(name)
	return ug_require(MOD .. "::/hermes_bridge/" .. name .. ".lua")
end

local json = load("json")
local util = load("util")
local snapshot = load("snapshot")(util)
local ops = load("ops")(util, snapshot)
local bridge = load("bridge")(json, util, ops)

-- A bug must not spam the log every frame: after repeated failures the GUI
-- loop backs off for about a minute, then tries again.
local MAX_ERRORS = 20
local BACKOFF_FRAMES = 3600
local errors, backoff = 0, 0

local script = {}

function script.update(userParams, state, dt)
	pcall(bridge.engine_update, state)
end

function script.handleEvent(userParams, state, src, id, name, param)
	local ok, err = pcall(bridge.engine_handle_event, state, src, id, name, param)
	if not ok and debugPrint then debugPrint("hermes_bridge handleEvent: " .. tostring(err)) end
end

function script.guiUpdate(userParams, stateReadOnly, guiState)
	if backoff > 0 then
		backoff = backoff - 1
		return
	end
	local ok, err = pcall(bridge.gui_tick, stateReadOnly, guiState)
	if ok then
		errors = 0
		return
	end
	errors = errors + 1
	if debugPrint then debugPrint("hermes_bridge guiUpdate: " .. tostring(err)) end
	if errors >= MAX_ERRORS then
		errors, backoff = 0, BACKOFF_FRAMES
	end
end

-- .script.lua resources expose their exports through data().
function data()
	return script
end
