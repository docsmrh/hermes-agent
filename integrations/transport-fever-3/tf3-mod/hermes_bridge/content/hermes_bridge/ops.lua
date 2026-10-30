-- Hermes Bridge op table.
--
-- Every op has the shape  fn(args, finish)  and must call
-- finish(true, result) or finish(false, {code=..., message=...}) exactly once,
-- either immediately (reads) or from an api.cmd.sendCommand callback.
--
-- Where ops run (see M.ENGINE_OPS): reads, camera, screenshots, saving and
-- lua_eval run in the GUI state (app and api.gui exist only there).
-- State-changing commands run in the engine (simulation) state, reached via
-- a script event: there commands execute synchronously, and construction
-- proposals convert reliably (community mods report proposal conversion with
-- removals failing from the GUI). set_speed stays in the GUI so it can unpause
-- a paused game, as the base game's own speed controls do.
--
-- Command factories and argument orders follow the official reference
-- (https://wiki.transportfever3.com/script-doc/api/cmd.html) and base-game
-- call sites. Ops marked EXPERIMENTAL build engine structs whose exact
-- construction is not documented end to end; run them with dry_run first
-- and check `selftest`.
--
-- Keep the op names in sync with relay/tf3_relay.py (allowlist) and the
-- Hermes plugin (guardrails.ACTION_CLASSES).

-- Loaded by hermes_bridge.script.lua as a factory: ops(util, snapshot) -> module.

return function(util, snapshot)

local M = {}

local VERSION = '0.1.0'
M.VERSION = VERSION

local function fail(finish, code, message)
  finish(false, { code = code, message = message })
end

local function need_int(args, key)
  local v = tonumber(args[key])
  if v == nil or v % 1 ~= 0 then
    error({ code = 'invalid_argument', message = key .. ' must be an integer' }, 0)
  end
  return v
end

local function need_fn(dotted)
  local fn = util.path(api, dotted)
  if type(fn) ~= 'function' then
    error({ code = 'api_unavailable', message = 'api.' .. dotted .. ' is not available in this game build' }, 0)
  end
  return fn
end

local function exists(entity)
  return snapshot.alive(entity)
end

local function need_entity(args, key, component_name)
  local id = need_int(args, key)
  if not exists(id) then
    error({ code = 'not_found', message = key .. ' ' .. id .. ' does not exist' }, 0)
  end
  if component_name and snapshot.component(id, component_name) == nil then
    error({ code = 'invalid_argument', message = key .. ' ' .. id .. ' is not a ' .. component_name }, 0)
  end
  return id
end

local function vec3f(t, what)
  if type(t) ~= 'table' or type(t[1]) ~= 'number' or type(t[2]) ~= 'number' then
    error({ code = 'invalid_argument', message = what .. ' must be [x, y, z]' }, 0)
  end
  return need_fn('type.Vec3f.new')(t[1], t[2], t[3] or 0)
end

-- Send a command; report success/failure through finish. `extract` maps the
-- executed command data to the op's result.
local function send(cmd, finish, extract)
  local sendCommand = need_fn('cmd.sendCommand')
  sendCommand(cmd, function(data, success, result_entities)
    if not success then
      fail(finish, 'command_failed', 'the game rejected the command')
      return
    end
    local result = { accepted = true }
    if extract then
      local ok, extra = pcall(extract, data, result_entities)
      if ok and type(extra) == 'table' then
        for k, v in pairs(extra) do result[k] = v end
      end
    end
    finish(true, result)
  end)
end

local function current_year()
  return util.try(util.path(api, 'engine.util.getYear'))
end

local function game_time()
  local gt = snapshot.world_component('GAME_TIME')
  return gt and util.try(function() return gt.gameTime end)
end

local function dry(args, finish, description)
  if args.dry_run == true then
    finish(true, { dry_run = true, valid = true, would = description })
    return true
  end
  return false
end

-- Reads ----------------------------------------------------------------------

M.ops = {}
local ops = M.ops

function ops.hello(args, finish)
  finish(true, { mod_version = VERSION, protocol = 1 })
end

function ops.ping(args, finish)
  finish(true, { pong = true })
end

function ops.snapshot(args, finish)
  finish(true, snapshot.snapshot(args))
end

function ops.get_entity(args, finish)
  finish(true, snapshot.entity(need_int(args, 'id')))
end

-- Probe which documented API entry points exist on this game build, so the
-- agent (and the user) can see what will work before trying it.
function ops.selftest(args, finish)
  local probes = {
    'engine.getComponent', 'engine.forEachEntityWithComponent', 'engine.getEntitiesWithComponent',
    'engine.entityExists', 'engine.util.getPlayer', 'engine.util.getWorld', 'engine.util.getEntityName',
    'engine.util.getCalendarDate', 'engine.util.finance.getPlayersBalance', 'engine.util.vehicle.getVehicles',
    'engine.system.lineSystem.getLinesForPlayer', 'engine.system.lineSystem.getProblemLines',
    'engine.system.transportVehicleSystem.getLineVehicles', 'engine.system.stationGroupSystem.getStationGroup',
    'cmd.sendCommand', 'cmd.makeGameSetSpeedCmd', 'cmd.makeLineCreateCmd', 'cmd.makeLineUpdateCmd',
    'cmd.makeLineDestroyCmd', 'cmd.makeVehicleBuyCmd', 'cmd.makeVehicleSetLineCmd', 'cmd.makeVehicleSellCmd',
    'cmd.makeVehicleSendToDepotCmd', 'cmd.makeVehicleSetStoppedByUserCmd', 'cmd.makeWorldBuildProposalCmd',
    'cmd.makeEntitySetNameCmd', 'type.Line.new', 'type.Line.Stop.new', 'type.TransportVehicleConfig.new',
    'type.TransportVehiclePart.new', 'type.SimpleProposal.new', 'type.SimpleProposal.ConstructionEntity.new',
    'type.Vec3f.new', 'type.Vec4f.new', 'type.Mat4f.new', 'res.modelRep.find',
    'res.modelRep.forEachModelWithMetadata', 'gui.camera.focusEntity', 'gui.camera.focusPosition',
    'gui.camera.takeScreenshot', 'app.saveGame', 'app.getUserDataFolder',
  }
  local available, missing = {}, {}
  for _, p in ipairs(probes) do
    if type(util.path(api, p)) == 'function' or (p:sub(1, 4) == 'app.' and type(util.path(app, p:sub(5))) == 'function') then
      available[#available + 1] = p
    else
      missing[#missing + 1] = p
    end
  end
  -- Line stop station index base: if any existing stop uses an index equal to
  -- its group's size, the index is 1-based; otherwise we assume 0-based.
  local index_base = 'unknown'
  local lines = snapshot.snapshot({ sections = { 'lines' }, limit = 20 }).lines
  for _, line in ipairs((lines and lines.items) or {}) do
    for _, stop in ipairs(line.stops or {}) do
      local group = stop.station_group_id and snapshot.component(stop.station_group_id, 'STATION_GROUP')
      local n = group and #util.list(util.try(function() return group.stations end)) or nil
      if n and stop.station_index ~= nil then
        if stop.station_index >= n then index_base = 1 elseif stop.station_index == 0 then index_base = 0 end
      end
    end
  end
  finish(true, { mod_version = VERSION, available = available, missing = missing,
                 line_stop_index_base = index_base })
end

function ops.list_vehicle_models(args, finish)
  local each = need_fn('res.modelRep.forEachModelWithMetadata')
  local find = util.path(api, 'res.modelRep.find')
  local filter = type(args.contains) == 'string' and args.contains:lower() or nil
  local limit = util.clamp_int(args.limit, 1, 500, 200)
  local models, total = {}, 0
  each(args.metadata or 'transportVehicle', function(file_name)
    total = total + 1
    if #models < limit and (filter == nil or tostring(file_name):lower():find(filter, 1, true)) then
      models[#models + 1] = { file = file_name, id = find and util.try(find, file_name) or nil }
    end
  end)
  finish(true, { models = models, total = total })
end

-- Camera / screenshots (view ops: no game-state change) -----------------------

function ops.camera_focus(args, finish)
  if args.entity_id ~= nil then
    need_fn('gui.camera.focusEntity')(need_int(args, 'entity_id'))
  else
    local distance = tonumber(args.distance) or 500
    need_fn('gui.camera.focusPosition')(vec3f(args.position, 'position'), distance)
  end
  finish(true, { focused = true })
end

function ops.take_screenshot(args, finish)
  need_fn('gui.camera.takeScreenshot')(util.clamp_int(args.scale, 1, 4, 1))
  finish(true, { requested = true, note = 'written to the userdata screenshot folder by the game' })
end

-- Actions ---------------------------------------------------------------------

function ops.set_speed(args, finish)
  local speedup = util.clamp_int(args.speedup, 0, 4, nil)
  if speedup == nil then return fail(finish, 'invalid_argument', 'speedup must be an integer 0-4 (0 = pause)') end
  local max_fn = util.path(api, 'gui.game.getEstimatedMaximumGameSpeed')
  local max_speed = max_fn and util.try(max_fn)
  if type(max_speed) == 'number' and speedup > max_speed then speedup = max_speed end
  local cmd = need_fn('cmd.makeGameSetSpeedCmd')(speedup)
  if dry(args, finish, { speedup = speedup }) then return end
  send(cmd, finish, function() return { speedup = speedup } end)
end

-- Resolve [{station_id=..}|{station_group_id=.., station_index=..}] into Line.Stop structs.
local function build_stops(list)
  if type(list) ~= 'table' or #list < 2 then
    error({ code = 'invalid_argument', message = 'stops needs at least 2 entries' }, 0)
  end
  local sgs = util.path(api, 'engine.system.stationGroupSystem')
  local new_stop = need_fn('type.Line.Stop.new')
  local stops = {}
  for i, s in ipairs(list) do
    if type(s) == 'number' then s = { station_id = s } end
    if type(s) ~= 'table' then error({ code = 'invalid_argument', message = 'stop ' .. i .. ' must be an object' }, 0) end
    local group, index = s.station_group_id, s.station_index
    if s.station_id ~= nil then
      local st = need_entity(s, 'station_id', 'STATION')
      group = sgs and util.try(sgs.getStationGroup, st)
      local comp = group and snapshot.component(group, 'STATION_GROUP')
      for j, member in ipairs(util.list(comp and util.try(function() return comp.stations end))) do
        if member == st then index = j - 1 end
      end
    end
    if group == nil or index == nil then
      error({ code = 'invalid_argument', message = 'stop ' .. i .. ': could not resolve station group/index' }, 0)
    end
    local stop = new_stop()
    stop.stationGroup = group
    stop.station = index
    stop.terminal = tonumber(s.terminal) or 0
    stops[#stops + 1] = stop
  end
  return stops
end

local function color3(c)
  if type(c) == 'table' and type(c[1]) == 'number' then
    return need_fn('type.Vec3f.new')(c[1], c[2] or 0, c[3] or 0)
  end
  return need_fn('type.Vec3f.new')(0.2, 0.6, 1.0)
end

function ops.line_create(args, finish)
  if type(args.name) ~= 'string' or args.name == '' then
    return fail(finish, 'invalid_argument', 'name is required')
  end
  local line = need_fn('type.Line.new')()
  line.stops = build_stops(args.stops)
  local cmd = need_fn('cmd.makeLineCreateCmd')(args.name, color3(args.color), snapshot.player(), line)
  if dry(args, finish, { name = args.name, stops = #line.stops }) then return end
  send(cmd, finish, function(data) return { line_id = util.try(function() return data.resultEntity end) } end)
end

function ops.line_update(args, finish)
  local line_id = need_entity(args, 'line_id', 'LINE')
  if args.stops == nil and args.name == nil then
    return fail(finish, 'invalid_argument', 'give stops and/or name')
  end
  local line_cmd
  if args.stops ~= nil then
    local line = need_fn('type.Line.new')(snapshot.component(line_id, 'LINE'))
    line.stops = build_stops(args.stops)
    line_cmd = need_fn('cmd.makeLineUpdateCmd')(line_id, line)
  end
  local name_cmd = args.name ~= nil and need_fn('cmd.makeEntitySetNameCmd')(line_id, tostring(args.name)) or nil
  if dry(args, finish, { line_id = line_id, stops = args.stops ~= nil, name = args.name }) then return end
  if line_cmd and name_cmd then
    send(line_cmd, function(ok, res)
      if not ok then return finish(ok, res) end
      send(name_cmd, finish)
    end)
  else
    send(line_cmd or name_cmd, finish)
  end
end

function ops.line_delete(args, finish)
  local line_id = need_entity(args, 'line_id', 'LINE')
  local cmd = need_fn('cmd.makeLineDestroyCmd')(line_id)
  if dry(args, finish, { line_id = line_id }) then return end
  send(cmd, finish)
end

local function model_id(spec)
  if type(spec) == 'number' then return spec end
  if type(spec) ~= 'string' or spec == '' then
    error({ code = 'invalid_argument', message = 'model must be a model file name or id' }, 0)
  end
  local id = util.try(need_fn('res.modelRep.find'), spec)
  if type(id) ~= 'number' or id < 0 then
    error({ code = 'not_found', message = 'unknown vehicle model ' .. spec .. ' (see list_vehicle_models)' }, 0)
  end
  return id
end

-- Build one TransportVehiclePart. Prefer the base game's own helper
-- (/gui/line_vehicle_mgmt/vehicle_util.tl, used the same way by working
-- community mods); fall back to assembling the documented struct by hand.
local function make_part(id)
  local vu = util.try(ug_require, '/gui/line_vehicle_mgmt/vehicle_util.tl')
  local part = vu and util.try(vu.makePart, id, true, nil)
  if part == nil then
    part = need_fn('type.TransportVehiclePart.new')()
    part.part.modelId = id
  end
  local t = game_time()
  if type(t) == 'number' then pcall(function() part.purchaseTime = math.floor(t) end) end
  return part
end

-- EXPERIMENTAL: TransportVehicleConfig assembly follows the documented fields
-- (vehicles, vehicleGroups, muFileNames); unverified on the live game.
function ops.vehicle_buy(args, finish)
  local depot = need_entity(args, 'depot_id', 'VEHICLE_DEPOT')
  local parts_spec = args.models or { args.model }
  if type(parts_spec) ~= 'table' or #parts_spec == 0 or #parts_spec > 40 then
    return fail(finish, 'invalid_argument', 'model (or models: up to 40 parts for a train) is required')
  end
  local parts = {}
  for _, spec in ipairs(parts_spec) do
    parts[#parts + 1] = make_part(model_id(spec))
  end
  local tvc = need_fn('type.TransportVehicleConfig.new')()
  tvc.vehicles = parts
  tvc.vehicleGroups = { #parts }
  pcall(function() tvc.muFileNames = {} end)
  local line_id = args.line_id ~= nil and need_entity(args, 'line_id', 'LINE') or nil
  local cmd = need_fn('cmd.makeVehicleBuyCmd')(snapshot.player(), depot, tvc)
  if dry(args, finish, { depot_id = depot, parts = #parts, line_id = line_id }) then return end
  send(cmd, function(ok, res)
    if not ok or line_id == nil or res.vehicle_id == nil then return finish(ok, res) end
    send(need_fn('cmd.makeVehicleSetLineCmd')(res.vehicle_id, line_id, 0), function(ok2, res2)
      res.line_assigned = ok2
      if not ok2 then res.line_error = res2 end
      finish(true, res)
    end)
  end, function(data) return { vehicle_id = util.try(function() return data.resultVehicleEntity end) } end)
end

function ops.vehicle_set_line(args, finish)
  local v = need_entity(args, 'vehicle_id', 'TRANSPORT_VEHICLE')
  local line_id = need_entity(args, 'line_id', 'LINE')
  local cmd = need_fn('cmd.makeVehicleSetLineCmd')(v, line_id, util.clamp_int(args.stop_index, 0, 1000, 0))
  if dry(args, finish, { vehicle_id = v, line_id = line_id }) then return end
  send(cmd, finish)
end

function ops.vehicle_sell(args, finish)
  local v = need_entity(args, 'vehicle_id', 'TRANSPORT_VEHICLE')
  local cmd = need_fn('cmd.makeVehicleSellCmd')({ v })
  if dry(args, finish, { vehicle_id = v }) then return end
  send(cmd, finish)
end

function ops.vehicle_to_depot(args, finish)
  local v = need_entity(args, 'vehicle_id', 'TRANSPORT_VEHICLE')
  local cmd = need_fn('cmd.makeVehicleSendToDepotCmd')(v, args.sell_on_arrival == true)
  if dry(args, finish, { vehicle_id = v }) then return end
  send(cmd, finish)
end

local function set_stopped(args, finish, stopped)
  local v = need_entity(args, 'vehicle_id', 'TRANSPORT_VEHICLE')
  local cmd = need_fn('cmd.makeVehicleSetStoppedByUserCmd')(v, stopped)
  if dry(args, finish, { vehicle_id = v, stopped = stopped }) then return end
  send(cmd, finish)
end

function ops.vehicle_stop(args, finish) set_stopped(args, finish, true) end
function ops.vehicle_start(args, finish) set_stopped(args, finish, false) end

-- Rotation about Z plus translation, as a Mat4f (column vectors).
local function transform(position, rotation)
  local p = position
  if type(p) ~= 'table' or type(p[1]) ~= 'number' or type(p[2]) ~= 'number' then
    error({ code = 'invalid_argument', message = 'position must be [x, y, z]' }, 0)
  end
  local c, s = math.cos(tonumber(rotation) or 0), math.sin(tonumber(rotation) or 0)
  local v4 = need_fn('type.Vec4f.new')
  return need_fn('type.Mat4f.new')(v4(c, s, 0, 0), v4(-s, c, 0, 0), v4(0, 0, 1, 0), v4(p[1], p[2], p[3] or 0, 1))
end

local function proposal_errors(data)
  local es = data and util.try(function() return data.errorState end)
  if es == nil then return nil end
  return {
    critical = util.try(function() return es.critical end),
    messages = util.plain(util.try(function() return es.messages end)),
  }
end

local function build_proposal(proposal, args, finish, description)
  if args.dry_run == true then
    -- Let the game validate (and price) the proposal without building it.
    local data = util.try(util.path(api, 'engine.util.proposal.makeProposalData'), proposal, nil)
    local errors = proposal_errors(data)
    local costs = data and util.try(function() return data.costs end)
    finish(true, { dry_run = true, valid = data ~= nil and not (errors and errors.critical),
                   errors = errors, costs = util.plain(costs), would = description })
    return
  end
  -- (proposal, context, ignoreErrors, playerInitiated): the four-argument
  -- form used by working community mods from the simulation side.
  local cmd = need_fn('cmd.makeWorldBuildProposalCmd')(proposal, nil, args.ignore_errors == true, true)
  local sendCommand = need_fn('cmd.sendCommand')
  sendCommand(cmd, function(data, success, result_entities)
    if not success then
      local errors = proposal_errors(util.try(function() return data.resultProposalData end))
      finish(false, { code = 'command_failed', message = 'the game rejected the proposal', errors = errors })
      return
    end
    local ids = {}
    for _, pair in ipairs(util.list(result_entities)) do
      local e = util.try(function() return pair[1] end)
      if e ~= nil then ids[#ids + 1] = e end
    end
    finish(true, { accepted = true, entity_ids = ids })
  end)
end

-- EXPERIMENTAL: construction params are construction-specific (see the
-- construction's .con file, e.g. "::/depots/road/road_depot/road_depot.con");
-- stations and depots are constructions. params must be complete: the game
-- copies the table on assignment, so later edits are lost.
function ops.build_construction(args, finish)
  if type(args.file) ~= 'string' or args.file == '' then
    return fail(finish, 'invalid_argument', 'file (construction resource, e.g. a depot or station .con) is required')
  end
  local params = {}
  if type(args.params) == 'table' then
    for k, v in pairs(args.params) do params[k] = v end
  end
  if params.year == nil then params.year = current_year() end
  if params.seed == nil then params.seed = 0 end
  local proposal = need_fn('type.SimpleProposal.new')()
  local ce = need_fn('type.SimpleProposal.ConstructionEntity.new')()
  ce.fileName = args.file
  ce.params = params
  ce.transf = transform(args.position, args.rotation)
  ce.playerEntity = snapshot.player()
  if type(args.name) == 'string' then ce.name = args.name end
  proposal.constructionsToAdd = { ce }
  build_proposal(proposal, args, finish, { file = args.file, position = args.position })
end

function ops.bulldoze(args, finish)
  local id = need_entity(args, 'entity_id', 'CONSTRUCTION')
  local proposal
  local remove = util.path(api, 'engine.util.proposal.createProposalRemove')
  local ctx_new = util.path(api, 'type.Context.new')
  if remove and ctx_new then proposal = util.try(remove, id, ctx_new()) end
  if proposal == nil then
    proposal = need_fn('type.SimpleProposal.new')()
    proposal.constructionsToRemove = { id }
  end
  build_proposal(proposal, args, finish, { entity_id = id })
end

function ops.save_game(args, finish)
  local name = type(args.name) == 'string' and args.name or 'hermes-autosave'
  if not name:match('^[%w%-_ ]+$') or #name > 64 then
    return fail(finish, 'invalid_argument', 'name may contain letters, digits, space, - and _ (max 64)')
  end
  local save = util.path(app, 'saveGame')
  if type(save) ~= 'function' then return fail(finish, 'api_unavailable', 'app.saveGame is not available') end
  if dry(args, finish, { name = name }) then return end
  local ok, err = pcall(save, name)
  if not ok then return fail(finish, 'command_failed', tostring(err)) end
  finish(true, { saved = name })
end

-- Advanced escape hatch; the relay only forwards it when allow_lua is on.
function ops.lua_eval(args, finish)
  if type(args.code) ~= 'string' then return fail(finish, 'invalid_argument', 'code is required') end
  local printed = {}
  local env = setmetatable({
    print = function(...)
      local parts = {}
      for i = 1, select('#', ...) do parts[i] = tostring((select(i, ...))) end
      if #printed < 200 then printed[#printed + 1] = table.concat(parts, '\t') end
    end,
  }, { __index = _ENV })
  local chunk, err = load(args.code, '=hermes_lua', 't', env)
  if not chunk then return fail(finish, 'lua_syntax', tostring(err)) end
  local ok, res = pcall(chunk)
  if not ok then return fail(finish, 'lua_error', tostring(res)) end
  finish(true, { value = util.plain(res), printed = printed })
end

-- Ops that must run in the engine (simulation) state.
M.ENGINE_OPS = {
  line_create = true, line_update = true, line_delete = true,
  vehicle_buy = true, vehicle_set_line = true, vehicle_sell = true, vehicle_to_depot = true,
  vehicle_stop = true, vehicle_start = true,
  build_construction = true, bulldoze = true,
}

-- Dispatch one request; never raises.
function M.dispatch(op, args, finish)
  local fn = ops[op]
  if fn == nil then
    return fail(finish, 'unknown_op', 'the mod does not implement op ' .. tostring(op))
  end
  local done = false
  local function once(ok, payload)
    if done then return end
    done = true
    finish(ok, payload)
  end
  local ok, err = pcall(fn, type(args) == 'table' and args or {}, once)
  if not ok and not done then
    if type(err) == 'table' and err.code then
      once(false, { code = err.code, message = err.message })
    else
      once(false, { code = 'internal', message = tostring(err) })
    end
  end
end

return M

end
