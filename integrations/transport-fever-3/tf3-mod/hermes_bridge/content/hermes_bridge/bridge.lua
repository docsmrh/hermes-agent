-- Hermes Bridge transport and dispatch loop.
--
-- GUI state (gui_tick, once per frame):
--   * talks to the Mac relay through the official userdata API
--     (app.saveUserdata / app.loadUserdata) in <userdata>/heightmaps/ — the
--     GUI Lua has no io library, and since build 40420 saveUserdata only
--     accepts heightmaps, mod_presets and towns_industries. heightmaps is the
--     one no in-game list shows (the map editor lists only images there);
--   * enforces the Mac-side policy the relay publishes in every inbox
--     (kill switch, allow_actions/allow_save/allow_lua), so a request that
--     was queued before the user paused automation is refused, not run;
--   * runs reads, camera, screenshots, saving and lua_eval itself;
--   * forwards state-changing ops to the engine state as a script event with
--     an absolute deadline, and collects their results from the game-script
--     state.
-- Engine state (engine_handle_event): executes a forwarded op at most once
-- per request id, and never after its deadline, so a command stuck behind a
-- paused or stalled simulation can not surprise anyone later.
--
-- Files (PROTOCOL.md section B) in <userdata>/heightmaps/, all Lua tables
-- {v=1, parts={json chunks}}:
--   hermes_bridge__in_<stamp>       relay -> mod, rewritten by the relay
--   hermes_bridge__out_<stamp>_<k>  mod -> relay, a fresh name per write
-- <stamp> is fresh every time this Lua state initialises (in practice: every
-- game load). Only fresh names are ever written by the mod, which sidesteps
-- reported saveUserdata overwrite/caching quirks, and requests carry the
-- stamp they were queued for so nothing crosses from one game into another.
--
-- Loaded by hermes_bridge.script.lua as a factory: bridge(json, util, ops).

return function(json, util, ops)

local M = {}

local PROTOCOL = 1
local DIR = 'heightmaps'
local PREFIX = 'hermes_bridge__'
local EVENT_ID = 'hermes_bridge'
local EVENT_NAME = 'hermes_bridge_op'
local PART_CHARS = 60000
local BUSY_POLL_FRAMES = 6         -- ~100 ms at 60 fps while requests are in flight
local IDLE_POLL_FRAMES = 15        -- ~250 ms otherwise
local HEARTBEAT_S = 3              -- outbox heartbeat while the relay is alive
local RELAY_ALIVE_S = 30           -- relay counts as alive this long after an inbox change
local ENGINE_DEADLINE_S = 75       -- a forwarded op must start within this, or it never runs
local ENGINE_GRACE_S = 5           -- then the GUI reports the (definitive) timeout
local RETRY_INIT_FRAMES = 600      -- retry setup every ~10 s after a failure
local MAX_DISPATCH_PER_POLL = 4
local MAX_ENGINE_RESULTS = 40
local MAX_RESULT_CHARS = 512 * 1024
local MAX_INBOX_CHARS = 1024 * 1024
local OUTBOX_BUDGET_CHARS = 2 * 1024 * 1024  -- hold new work while this much awaits the relay

-- Request classes, mirroring the relay's allowlist. Reads and view ops run
-- regardless of policy; everything else is a state change.
local READ_OPS = { hello = true, ping = true, selftest = true, snapshot = true, get_entity = true,
                   list_vehicle_models = true }
local VIEW_OPS = { camera_focus = true, take_screenshot = true }
local SAVE_OPS = { save_game = true }
local LUA_OPS = { lua_eval = true }

M.EVENT_ID, M.EVENT_NAME, M.PREFIX, M.DIR = EVENT_ID, EVENT_NAME, PREFIX, DIR
M.GS_NAME = 'hermes_bridge::/hermes_bridge/hermes_bridge.gs'

-- Kept in package.loaded so the GUI-side state survives the game
-- re-evaluating the script file; nothing then depends on how often it does.
local S = {}
do
  local loaded = util.try(function() return package.loaded end)
  if type(loaded) == 'table' then
    if type(loaded.hermes_bridge_gui_state) == 'table' then S = loaded.hermes_bridge_gui_state end
    loaded.hermes_bridge_gui_state = S
  end
end

local function reset()
  S.frame = 0
  S.stamp = nil
  S.k = 0
  S.next_init = 0
  S.next_poll = 0
  S.next_heartbeat_t = 0
  S.inbox_text = nil
  S.inbox = nil
  S.relay_seen_t = nil
  S.session = nil
  S.seen = {}
  S.results = {}
  S.order = {}
  S.dirty = false
  S.status = 'starting'
  S.errors = 0
end
if S.frame == nil then reset() end

-- Userdata file helpers ------------------------------------------------------

local function save(name, tbl)
  local fn = util.path(app, 'saveUserdata')
  if type(fn) ~= 'function' then return false, 'app.saveUserdata unavailable' end
  local ok, err = pcall(fn, DIR, name, tbl)
  return ok, err
end

local function load_table(name)
  local fn = util.path(app, 'loadUserdata')
  if type(fn) ~= 'function' then return nil end
  local ok, res = pcall(fn, DIR, name)
  if ok and type(res) == 'table' then return res end
  return nil
end

local function remove(name)
  local fn = util.path(app, 'removeUserdata')
  if type(fn) == 'function' then pcall(fn, DIR, name) end
end

local function to_parts(text)
  local parts = {}
  for i = 1, #text, PART_CHARS do
    parts[#parts + 1] = text:sub(i, i + PART_CHARS - 1)
  end
  return parts
end

local function from_parts(tbl)
  if type(tbl) ~= 'table' or tbl.v ~= PROTOCOL or type(tbl.parts) ~= 'table' then return nil end
  local chunks, size = {}, 0
  for i = 1, #tbl.parts do
    if type(tbl.parts[i]) ~= 'string' then return nil end
    size = size + #tbl.parts[i]
    if size > MAX_INBOX_CHARS then return nil, 'too_large' end
    chunks[i] = tbl.parts[i]
  end
  return table.concat(chunks)
end

local function inbox_name() return PREFIX .. 'in_' .. S.stamp end
local function outbox_name(k) return PREFIX .. 'out_' .. S.stamp .. '_' .. k end

-- Results ---------------------------------------------------------------------

local function record(id, seq, op)
  local rec = { id = id, seq = seq, op = op, status = 'pending', size = 0 }
  S.results[id] = rec
  S.order[#S.order + 1] = id
  return rec
end

local function complete(rec, ok, payload)
  if rec.status == 'done' then return end
  rec.status = 'done'
  rec.ok = ok and true or false
  rec.forwarded, rec.deadline = nil, nil
  rec.size = 200
  if ok then
    local enc_ok, encoded = pcall(json.encode, payload)
    if not enc_ok then
      rec.ok = false
      rec.error = { code = 'unserialisable_result', message = tostring(encoded) }
    elseif #encoded > MAX_RESULT_CHARS then
      rec.ok = false
      rec.error = { code = 'result_too_large', message = 'result too large; request fewer items (limit)' }
    else
      rec.result = payload
      rec.size = #encoded
    end
  else
    rec.error = payload
  end
  S.dirty = true
end

local function prune(live)
  local kept = {}
  for _, id in ipairs(S.order) do
    if live[id] then
      kept[#kept + 1] = id
    else
      S.results[id] = nil
      S.seen[id] = nil
    end
  end
  S.order = kept
end

local function waiting_chars()
  local total = 0
  for _, id in ipairs(S.order) do
    local rec = S.results[id]
    if rec and rec.status == 'done' then total = total + (rec.size or 0) end
  end
  return total
end

local function write_outbox()
  local results = json.array()
  for _, id in ipairs(S.order) do
    local rec = S.results[id]
    if rec then
      results[#results + 1] = { id = rec.id, seq = rec.seq, status = rec.status, ok = rec.ok,
                                result = rec.result, error = rec.error }
    end
  end
  local ok, text = pcall(json.encode, {
    v = PROTOCOL,
    stamp = S.stamp,
    session = S.session or '',
    mod_version = ops.VERSION,
    transport = 'userdata',
    frame = S.frame,
    status = S.status,
    results = results,
  })
  if not ok then return end
  S.k = S.k + 1
  local written = save(outbox_name(S.k), { v = PROTOCOL, k = S.k, parts = to_parts(text) })
  if written and S.k > 2 then remove(outbox_name(S.k - 2)) end
  S.dirty = false
  S.next_heartbeat_t = util.now(S.frame) + HEARTBEAT_S
end

-- Setup -------------------------------------------------------------------------

-- A stamp unique to this Lua state: wall clock plus a random part seeded
-- from several clocks, so two inits in the same second still differ.
local function new_stamp()
  local t = util.try(util.path(os, 'time')) or 0
  local c = util.try(util.path(os, 'clock')) or 0
  local a = util.try(util.path(api, 'util.getApplicationTime')) or 0
  pcall(math.randomseed, (t % 1000000) + math.floor((c * 7919 + a * 104729) % 1000000) + S.frame)
  return string.format('%d%06d', t, math.random(0, 999999))
end

local function init()
  S.stamp = new_stamp()
  -- The inbox must exist before we poll it (loading a missing userdata file
  -- logs an error in the game log), so the mod creates it empty itself.
  local ok, err = save(inbox_name(), { v = PROTOCOL, parts = {} })
  if not ok then
    S.stamp = nil
    S.status = 'cannot write ' .. DIR .. ': ' .. tostring(err)
    return false
  end
  S.status = 'waiting for relay'
  write_outbox() -- doubles as the hello the relay discovers
  return true
end

-- Policy ------------------------------------------------------------------------

-- The relay is the authority; the mod re-checks every request against the
-- policy in the inbox it arrived in, and fails closed without one.
local function policy_denial(op, policy)
  if READ_OPS[op] or VIEW_OPS[op] then return nil end
  if type(policy) ~= 'table' then
    return { code = 'policy_denied', message = 'no policy from the relay; refusing state changes' }
  end
  if policy.paused == true then
    return { code = 'paused', message = 'automation is paused on the Mac; nothing was done' }
  end
  local flag = (LUA_OPS[op] and 'allow_lua') or (SAVE_OPS[op] and 'allow_save') or 'allow_actions'
  if policy[flag] ~= true then
    return { code = 'policy_denied', message = flag .. ' is off on the Mac; nothing was done' }
  end
  return nil
end

-- While the game is paused the simulation does not step, so a forwarded
-- command would only run whenever someone resumes the game. Refuse it up
-- front; whether to unpause is the player's call, not the agent's.
local function game_paused()
  local world = util.try(util.path(api, 'engine.util.getWorld'))
  local ct = util.path(api, 'type.ComponentType.GAME_SPEED')
  local exists = util.path(api, 'engine.entityExists')
  if world == nil or ct == nil or (exists and util.try(exists, world) ~= true) then return false end
  local gs = util.try(api.engine.getComponent, world, ct)
  return gs ~= nil and util.try(function() return gs.speedup == 0 end) == true
end

-- GUI loop ---------------------------------------------------------------------

local function forward(rec, op, args)
  if game_paused() then
    complete(rec, false, { code = 'game_paused', retryable = true,
      message = 'the game is paused, so nothing was done (commands only run while the simulation runs)' })
    return
  end
  local make = util.path(api, 'cmd.makeScriptingSendEventCmd')
  local sendCommand = util.path(api, 'cmd.sendCommand')
  if type(make) ~= 'function' or type(sendCommand) ~= 'function' then
    complete(rec, false, { code = 'api_unavailable', message = 'script events are unavailable' })
    return
  end
  local deadline = util.now(S.frame) + ENGINE_DEADLINE_S
  local param = { key = rec.id, op = op, args = args, expires_at = deadline }
  local ok, err = pcall(sendCommand, make('', EVENT_ID, EVENT_NAME, param))
  if not ok then
    complete(rec, false, { code = 'internal', message = 'could not forward to the simulation: ' .. tostring(err) })
    return
  end
  rec.forwarded = true
  rec.deadline = deadline
end

local function dispatch(rec, req, policy)
  local args = type(req.args) == 'table' and req.args or {}
  if req.stamp ~= S.stamp then
    -- Queued for another game load; never run it against this one.
    complete(rec, false, { code = 'stale_stamp', message = 'request was queued for a different game load' })
    return
  end
  local denial = policy_denial(req.op, policy)
  if denial then
    complete(rec, false, denial)
    return
  end
  if ops.ENGINE_OPS[req.op] and args.dry_run ~= true then
    forward(rec, req.op, args)
  else
    ops.dispatch(req.op, args, function(ok, payload) complete(rec, ok, payload) end)
  end
end

local function read_inbox()
  local text, err = from_parts(load_table(inbox_name()))
  if err == 'too_large' then S.status = 'inbox too large; ignoring it' end
  if text == nil then return nil end
  if text ~= S.inbox_text then
    S.inbox_text = text
    S.relay_seen_t = util.now(S.frame)
    S.inbox = nil
    if text ~= '' then
      local ok, inbox = pcall(json.decode, text)
      if ok and type(inbox) == 'table' and inbox.v == PROTOCOL and type(inbox.session) == 'string' then
        if type(inbox.requests) ~= 'table' then inbox.requests = {} end
        table.sort(inbox.requests, function(a, b) return (tonumber(a.seq) or 0) < (tonumber(b.seq) or 0) end)
        S.inbox = inbox
      end
    end
  end
  return S.inbox -- decoded once per change, not once per poll
end

local function poll()
  local inbox = read_inbox()
  if inbox == nil then return false end
  if inbox.session ~= S.session then
    -- A new relay session restarts the request stream.
    S.session = inbox.session
    S.seen, S.results, S.order = {}, {}, {}
    S.dirty = true
  end
  S.status = 'connected'
  local live, dispatched, busy = {}, 0, false
  local backlog = waiting_chars() > OUTBOX_BUDGET_CHARS
  for _, req in ipairs(inbox.requests) do
    local id = type(req) == 'table' and req.id or nil
    if type(id) == 'string' and type(req.op) == 'string' then
      live[id] = true
      if not S.seen[id] then
        if dispatched >= MAX_DISPATCH_PER_POLL or backlog then
          busy = true
        else
          S.seen[id] = true
          dispatched = dispatched + 1
          dispatch(record(id, req.seq, req.op), req, inbox.policy)
          S.dirty = true
        end
      end
      local rec = S.results[id]
      if rec and rec.status == 'pending' then busy = true end
    end
  end
  prune(live)
  return busy
end

local function engine_state(state_ro)
  local st = state_ro and util.try(function() return state_ro:get() end)
  if type(st) == 'table' then return st end
  -- Fallback: read the game-script state straight from its GAME_SCRIPT component.
  local find = util.path(api, 'engine.system.gameScriptSystem.getEntityForGameScript')
  local entity = find and util.try(find, M.GS_NAME)
  local exists = util.path(api, 'engine.entityExists')
  if type(entity) ~= 'number' or (exists and util.try(exists, entity) ~= true) then return nil end
  local ct = util.path(api, 'type.ComponentType.GAME_SCRIPT')
  local comp = ct and util.try(api.engine.getComponent, entity, ct)
  return comp and util.try(function() return comp.state end)
end

local function collect_engine_results(state_ro)
  local pending = false
  for _, id in ipairs(S.order) do
    local rec = S.results[id]
    if rec and rec.forwarded and rec.status == 'pending' then pending = true break end
  end
  if not pending then return end
  local st = engine_state(state_ro)
  local results = type(st) == 'table' and st.hermes_bridge_results or nil
  local now = util.now(S.frame)
  for _, id in ipairs(S.order) do
    local rec = S.results[id]
    if rec and rec.forwarded and rec.status == 'pending' then
      local res = type(results) == 'table' and results[id] or nil
      if type(res) == 'table' and res.done then
        complete(rec, res.ok, res.ok and res.result or res.error)
      elseif now > (rec.deadline or now) + ENGINE_GRACE_S then
        -- Past its deadline the engine refuses the command, so this is final.
        complete(rec, false, { code = 'engine_timeout', retryable = true,
          message = 'the simulation did not run the command in time; it has expired and will not run, '
            .. 'so nothing was done' })
      end
    end
  end
end

function M.gui_tick(state_ro, gui_state)
  S.frame = S.frame + 1
  if S.stamp == nil then
    if S.frame < S.next_init then return end
    S.next_init = S.frame + RETRY_INIT_FRAMES
    if not init() then return end
  end
  if S.frame >= S.next_poll then
    local ok, busy = pcall(poll)
    if not ok then
      S.errors = S.errors + 1
      busy = false
    end
    S.next_poll = S.frame + (busy and BUSY_POLL_FRAMES or IDLE_POLL_FRAMES)
  end
  collect_engine_results(state_ro)
  local now = util.now(S.frame)
  local relay_alive = S.relay_seen_t ~= nil and now - S.relay_seen_t <= RELAY_ALIVE_S
  if S.dirty or (relay_alive and now >= S.next_heartbeat_t) then
    pcall(write_outbox)
  end
end

-- Engine side ------------------------------------------------------------------

-- Script events are routed by NAME, and only to scripts subscribed to it.
-- Re-subscribing on every step is what the base game's own scripts do.
local function subscribe(state)
  pcall(function() state:subscribeToEvent(EVENT_NAME) end)
end

function M.engine_update(state)
  subscribe(state)
end

local function store_engine_result(state, key, ok, payload)
  local st = util.try(function() return state:get() end)
  if type(st) ~= 'table' then st = {} end
  local results = type(st.hermes_bridge_results) == 'table' and st.hermes_bridge_results or {}
  local order = type(st.hermes_bridge_order) == 'table' and st.hermes_bridge_order or {}
  results[key] = { done = true, ok = ok and true or false,
                   result = ok and util.plain(payload) or nil, error = (not ok) and util.plain(payload) or nil }
  order[#order + 1] = key
  while #order > MAX_ENGINE_RESULTS do
    results[table.remove(order, 1)] = nil
  end
  st.hermes_bridge_results, st.hermes_bridge_order = results, order
  pcall(function() state:set(st) end)
end

function M.engine_handle_event(state, src, id, name, param)
  if id == '' or id == nil then
    subscribe(state) -- lifecycle events (new game, load) arrive with an empty id
    return
  end
  if id ~= EVENT_ID or name ~= EVENT_NAME or type(param) ~= 'table' then return end
  local key = param.key
  if type(key) ~= 'string' then return end
  local st = util.try(function() return state:get() end)
  local seen = type(st) == 'table' and type(st.hermes_bridge_results) == 'table' and st.hermes_bridge_results[key]
  if seen then return end -- already executed: at most once per request id
  if type(param.expires_at) ~= 'number' or util.now(0) > param.expires_at then
    store_engine_result(state, key, false, { code = 'expired',
      message = 'the command reached the simulation after its deadline and was not run' })
    return
  end
  if not ops.ENGINE_OPS[param.op] then
    store_engine_result(state, key, false, { code = 'unknown_op', message = 'not an engine op: ' .. tostring(param.op) })
    return
  end
  ops.dispatch(param.op, type(param.args) == 'table' and param.args or {}, function(ok, payload)
    store_engine_result(state, key, ok, payload)
  end)
end

function M.status()
  return { stamp = S.stamp, status = S.status, session = S.session, frame = S.frame, k = S.k }
end

M._reset = reset

return M

end
