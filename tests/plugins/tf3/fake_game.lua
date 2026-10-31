-- A stand-in for the Transport Fever 3 script runtime, used to run the real
-- Hermes Bridge mod Lua under a stock Lua 5.2 interpreter in tests.
--
-- It models only what the mod touches: a tiny world (player, two stations
-- in separate groups, a line with two vehicles, a depot, a town, an
-- industry), api.cmd command objects executed on the next simulation step
-- (immediately when sent from the engine state), game-script state objects,
-- and app.*Userdata backed by real files in USERDATA, written in the
-- `function data() return {...} end` format with %q strings.
--
-- Usage from Python: rt.execute(this_file)(userdata_dir, native_vector_factory)
-- returns a table { api=, app=, step=, log=, engine_state=, gui_state= ... }.

return function(USERDATA, make_vector)

local G = {}

local CT_NAMES = {
  'ACCOUNT', 'GAME_TIME', 'GAME_SPEED', 'NAME', 'LINE', 'STATION', 'STATION_GROUP', 'TRANSPORT_VEHICLE',
  'VEHICLE_DEPOT', 'TOWN', 'INDUSTRY', 'CONSTRUCTION', 'PLAYER_OWNED', 'PLAYER', 'GAME_SCRIPT',
}
local CT = {}
for i, n in ipairs(CT_NAMES) do CT[n] = i end
local CT_BY_ID = {}
for n, i in pairs(CT) do CT_BY_ID[i] = n end

local PLAYER, WORLD = 1, 2
local LINE = 100
local ST_A, ST_B = 200, 201
local GROUP_A, GROUP_B = 300, 301
local DEPOT = 400
local VEH_1, VEH_2 = 500, 501
local TOWN = 600
local IND, IND_CON = 700, 701
local CON_A = 210

local next_entity = 1000

local components = {}
local function set(e, name, comp)
  components[e] = components[e] or {}
  components[e][name] = comp
end
local function owned(e) set(e, 'PLAYER_OWNED', { player = PLAYER }) end

set(PLAYER, 'ACCOUNT', { balance = 5000000, loan = 1000000 })
set(PLAYER, 'PLAYER', {})
set(WORLD, 'GAME_TIME', { gameTime = 3650000 })
set(WORLD, 'GAME_SPEED', { speedup = 1, millisPerDay = 2000 })
for e, n in pairs({ [LINE] = 'Bus 1', [ST_A] = 'Alpha Station', [ST_B] = 'Beta Station', [DEPOT] = 'Bus Depot',
                    [VEH_1] = 'Bus #1', [VEH_2] = 'Bus #2', [TOWN] = 'Alpha', [IND] = 'Coal Mine' }) do
  set(e, 'NAME', { name = n })
end
-- LINE.stops is a native vector (userdata with :size()/:at(1..n)) to
-- exercise the mod's vector handling; everything else is plain tables.
set(LINE, 'LINE', {
  stops = make_vector({ { stationGroup = GROUP_A, station = 0, terminal = 0 },
                        { stationGroup = GROUP_B, station = 0, terminal = 0 } }),
  vehicleInfo = { transportModes = { [3] = true } },  -- keyed by TransportMode enum values (3 = BUS)
})
owned(LINE)
set(ST_A, 'STATION', { terminals = { {}, {} } }); owned(ST_A)  -- two platforms: terminals 0 and 1
set(ST_B, 'STATION', { terminals = { {}, {} } }); owned(ST_B)
set(GROUP_A, 'STATION_GROUP', { stations = { ST_A } })
set(GROUP_B, 'STATION_GROUP', { stations = { ST_B } })
set(CON_A, 'CONSTRUCTION', { fileName = '::/stations/street/x.con', transf = { 1,0,0,0, 0,1,0,0, 0,0,1,0, 100,200,5,1 } })
set(DEPOT, 'VEHICLE_DEPOT', {}); owned(DEPOT)
for _, v in ipairs({ VEH_1, VEH_2 }) do
  set(v, 'TRANSPORT_VEHICLE', { line = LINE, depot = DEPOT, state = 1, carrier = 0, userStopped = false,
                                noPath = false, stopIndex = 0 })
  owned(v)
end
set(TOWN, 'TOWN', {})
set(IND, 'INDUSTRY', { construction = IND_CON, stockList = 702, level = 1, maxLevel = 3 })
set(IND_CON, 'CONSTRUCTION', { fileName = '::/industry/coal_mine.con', transf = { 1,0,0,0, 0,1,0,0, 0,0,1,0, -50,75,0,1 } })

local function exists(e) return components[e] ~= nil end

G.log = {}       -- executed commands, in order
G.events = {}    -- script events delivered to the engine
local queue = {}  -- commands sent from the GUI state, run on the next step
G.in_engine = false

-- Game-script state objects ------------------------------------------------

local function deepcopy(v)
  if type(v) ~= 'table' then return v end
  local out = {}
  for k, x in pairs(v) do out[k] = deepcopy(x) end
  return out
end

local function make_state()
  local store, subs = {}, {}
  local s = {}
  function s:get() return deepcopy(store) end
  function s:set(t) store = deepcopy(t) end
  function s:subscribeToEvent(name) subs[name] = true end
  function s:hasEventSubscriptions() return next(subs) ~= nil end
  s._subs = subs
  return s
end
G.engine_state = make_state()
G.gui_state = make_state()
G.engine_state_ro = { get = function(self) return G.engine_state:get() end }

-- api ------------------------------------------------------------------------

local api = { type = {}, engine = { util = {}, system = {} }, cmd = {}, res = {}, gui = { camera = {}, game = {} }, util = {} }
api.type.ComponentType = CT
api.type.enum = { TransportVehicleState = { IN_DEPOT = 0, EN_ROUTE = 1, AT_TERMINAL = 2, GOING_TO_DEPOT = 3 },
                  Carrier = { ROAD = 0, RAIL = 1, TRAM = 2, OTHER = 3, AIR = 4, WATER = 5 },
                  TransportMode = { PERSON = 0, CARGO = 1, CAR = 2, BUS = 3, TRUCK = 4, TRAM = 5 } }

-- The real engine aborts the whole game (not a Lua error) when asked for a
-- component of an entity that does not exist; count such reads so tests can
-- assert the mod never makes one.
G.fatal_reads = 0
function api.engine.getComponent(e, ct)
  local name = CT_BY_ID[ct]
  if name == nil then error('bad component type') end
  if components[e] == nil then
    G.fatal_reads = G.fatal_reads + 1
    return nil
  end
  return components[e][name]
end
function api.engine.entityExists(e) return exists(e) end
function api.engine.forEachEntityWithComponent(fn, ct)
  local name = CT_BY_ID[ct]
  local ids = {}
  for e, comps in pairs(components) do if comps[name] then ids[#ids + 1] = e end end
  table.sort(ids)
  for _, e in ipairs(ids) do fn(e) end
end
function api.engine.getEntitiesWithComponent(ct, filters)
  local out = {}
  api.engine.forEachEntityWithComponent(function(e)
    local po = components[e].PLAYER_OWNED
    if not (filters and filters.requireOwnedByPlayer) or (po and po.player == filters.requireOwnedByPlayer) then
      out[#out + 1] = e
    end
  end, ct)
  return out
end

local eu = api.engine.util
function eu.getPlayer() return PLAYER end
function eu.getWorld() return WORLD end
function eu.getEntityName(e) return components[e] and components[e].NAME and components[e].NAME.name or nil end
function eu.getCalendarDate(t) return { year = 1900 + math.floor(t / 730000), month = 3, day = 14 } end
function eu.getYear() return 1905 end
eu.finance = {
  getPlayersBalance = function(p) return components[p].ACCOUNT.balance end,
  calculateEarnings = function(p) return 123456 end,
  calculateBalance = function(entities, from, to, flag) return -4200 end,
}
eu.vehicle = {
  getVehicles = function() return { VEH_1, VEH_2 } end,
  getPosition = function(v) return { x = 10.04, y = 20.06, z = 1 } end,
  getRunningCost = function(v) return 15000 end,
  getDepreciatedValue = function(v) return 40000 end,
}
-- A map keyed by cargo type, as in the game (not a list).
eu.line = { getLineCapacityUsages = function(l, flag) return { PASSENGERS = { used = 30, capacity = 40 } } end }
eu.town = { getTownCapacityUsage = function(t) return { { used = 900, capacity = 1000 }, { used = 50, capacity = 80 } } end }
eu.stock = { getCargoShippedPerYear = function(s) return 1200 end, getProductionRating = function(s) return 0.8 end }
eu.proposal = {
  makeProposalData = function(p, ctx) return { errorState = { critical = false, messages = {} }, costs = 75000 } end,
}
eu.station = { calculateStationUsage = function(st) return { totalUsed = 12, poolCapacity = 50 } end }

local sys = api.engine.system
sys.lineSystem = {
  getLinesForPlayer = function(p)
    local out = {}
    api.engine.forEachEntityWithComponent(function(e)
      local po = components[e].PLAYER_OWNED
      if po and po.player == p then out[#out + 1] = e end
    end, CT.LINE)
    return out
  end,
  getProblemLines = function(p) return { { LINE, 2 } } end,
  getLinesForStationGroup = function(g) return { LINE } end,
}
sys.transportVehicleSystem = {
  getLineVehicles = function(l) return l == LINE and { VEH_1, VEH_2 } or {} end,
  getNoPathVehicles = function() return { VEH_2 } end,
  getDepotVehicles = function(d) return {} end,
}
sys.stationGroupSystem = { getStationGroup = function(st) return st == ST_A and GROUP_A or GROUP_B end }
sys.stationSystem = {
  getTown = function(st) return TOWN end,
  getStations = function(t) return { ST_A, ST_B } end,
}
sys.streetConnectorSystem = { getConstructionEntityForStation = function(st) return st == ST_A and CON_A or nil end }
api.util.getYearDuration = function(mpd) return mpd * 365 end

-- Types
api.type.Vec3f = { new = function(x, y, z) return { x = x, y = y, z = z } end }
api.type.Vec4f = { new = function(a, b, c, d) return { a, b, c, d } end }
api.type.Mat4f = { new = function(c1, c2, c3, c4) return { c1, c2, c3, c4 } end }
api.type.Line = {
  new = function(copy)
    local l = { stops = {}, vehicleInfo = { transportModes = {} } }
    if copy then l.vehicleInfo = copy.vehicleInfo end
    return l
  end,
  Stop = { new = function() return { stationGroup = -1, station = -1, terminal = -1 } end },
}
api.type.TransportVehiclePart = { new = function() return { part = { modelId = -1 }, purchaseTime = 0 } end }
api.type.TransportVehicleConfig = { new = function() return { vehicles = {}, vehicleGroups = {} } end }
api.type.SimpleProposal = {
  new = function() return { constructionsToAdd = {}, constructionsToRemove = {}, streetProposal = {} } end,
  ConstructionEntity = { new = function() return { params = {} } end },
}
api.type.Context = { new = function() return {} end }

local MODELS = { ['vehicle/bus/old_bus.mdl'] = 77, ['vehicle/tram/old_tram.mdl'] = 78 }
local MODEL_YEARS = { [77] = { 1880, 1930 }, [78] = { 1950, 0 } }
api.res.modelRep = {
  find = function(name) return MODELS[name] or -1 end,
  get = function(id)
    local y = MODEL_YEARS[id]
    return { metadata = { availability = { yearFrom = y[1], yearTo = y[2] }, cost = { price = 60000 } } }
  end,
  forEachModelWithMetadata = function(meta, fn)
    for _, n in ipairs({ 'vehicle/bus/old_bus.mdl', 'vehicle/tram/old_tram.mdl' }) do fn(n) end
  end,
}

api.gui.game.getEstimatedMaximumGameSpeed = function() return 4 end
G.camera = {}
api.gui.camera.focusEntity = function(e) G.camera.entity = e end
api.gui.camera.focusPosition = function(p, d) G.camera.position = p; G.camera.distance = d end
api.gui.camera.takeScreenshot = function(scale)
  local f = assert(io.open(USERDATA .. '/screenshots/shot_' .. os.time() .. '.png', 'wb'))
  f:write('\137PNG\r\n\26\nFAKE')
  f:close()
end

-- Commands
local function cmd(kind, fields) fields.kind = kind; return fields end
api.cmd.makeGameSetSpeedCmd = function(s) return cmd('speed', { speedup = s }) end
api.cmd.makeLineCreateCmd = function(name, color, player, line) return cmd('line_create', { name = name, color = color, player = player, line = line }) end
api.cmd.makeLineUpdateCmd = function(id, line) return cmd('line_update', { id = id, line = line }) end
api.cmd.makeLineDestroyCmd = function(id) return cmd('line_destroy', { id = id }) end
api.cmd.makeEntitySetNameCmd = function(id, name) return cmd('set_name', { id = id, name = name }) end
api.cmd.makeVehicleBuyCmd = function(p, d, tvc) return cmd('vehicle_buy', { player = p, depot = d, config = tvc }) end
api.cmd.makeVehicleSetLineCmd = function(v, l, i) return cmd('vehicle_set_line', { vehicle = v, line = l, stop = i }) end
api.cmd.makeVehicleSellCmd = function(vs) return cmd('vehicle_sell', { vehicles = vs }) end
api.cmd.makeVehicleSendToDepotCmd = function(v, sell) return cmd('vehicle_to_depot', { vehicle = v, sell = sell }) end
api.cmd.makeVehicleSetStoppedByUserCmd = function(v, s) return cmd('vehicle_stopped', { vehicle = v, stopped = s }) end
api.cmd.makeWorldBuildProposalCmd = function(p, ctx, ign, pi) return cmd('proposal', { proposal = p, ignoreErrors = ign, playerInitiated = pi }) end
api.cmd.makeScriptingSendEventCmd = function(src, id, name, param) return cmd('event', { src = src, id = id, name = name, param = param }) end

G.script = nil       -- set by the harness after loading the mod
G.reject = {}        -- kind -> true makes the game refuse that command

local function execute(c, cb)
  G.log[#G.log + 1] = c
  if c.kind == 'event' then
    G.events[#G.events + 1] = c
    local was = G.in_engine
    G.in_engine = true
    G.script.handleEvent({}, G.engine_state, c.src, c.id, c.name, c.param)
    G.in_engine = was
    if cb then cb(c, true, {}) end
    return
  end
  if G.reject[c.kind] then
    if cb then cb(c, false, {}) end
    return
  end
  local ents = {}
  if c.kind == 'speed' then components[WORLD].GAME_SPEED.speedup = c.speedup end
  if c.kind == 'line_create' then
    next_entity = next_entity + 1
    c.resultEntity = next_entity
    set(next_entity, 'LINE', c.line); set(next_entity, 'NAME', { name = c.name }); owned(next_entity)
  end
  if c.kind == 'vehicle_buy' then
    next_entity = next_entity + 1
    c.resultVehicleEntity = next_entity
    set(next_entity, 'TRANSPORT_VEHICLE', { depot = c.depot, state = 0 }); owned(next_entity)
  end
  if c.kind == 'proposal' then
    for _, ce in ipairs(c.proposal.constructionsToAdd or {}) do
      next_entity = next_entity + 2
      local con, depot = next_entity - 1, next_entity
      local depots = {}
      if tostring(ce.fileName):find('depot', 1, true) then
        set(depot, 'VEHICLE_DEPOT', {}); owned(depot)
        depots = { depot }
      end
      set(con, 'CONSTRUCTION', { fileName = ce.fileName, transf = ce.transf, depots = depots, stations = {} })
      ents[#ents + 1] = { con, 0 }
    end
    if #ents == 0 then
      next_entity = next_entity + 1
      ents = { { next_entity, 0 } }
    end
  end
  if cb then cb(c, true, ents) end
end

api.cmd.sendCommand = function(c, cb)
  if G.in_engine then
    execute(c, cb)          -- engine state: synchronous
  else
    queue[#queue + 1] = { c, cb }  -- GUI state: next simulation step
  end
end

-- One simulation step: run queued GUI commands, then the engine update hook.
function G.step()
  local batch = queue
  queue = {}
  for _, item in ipairs(batch) do execute(item[1], item[2]) end
  if G.script then
    G.in_engine = true
    G.script.update({}, G.engine_state, 0.2)
    G.in_engine = false
  end
end

-- One rendered frame: the GUI hook.
function G.frame()
  G.script.guiUpdate({}, G.engine_state_ro, G.gui_state)
end

G.api = api
G.components = components
G.ids = { PLAYER = PLAYER, LINE = LINE, ST_A = ST_A, ST_B = ST_B, GROUP_A = GROUP_A, GROUP_B = GROUP_B,
          DEPOT = DEPOT, VEH_1 = VEH_1, VEH_2 = VEH_2, TOWN = TOWN, IND = IND, CON_A = CON_A }

-- app: userdata files ------------------------------------------------------------

local ALLOWED = { mod_presets = true, heightmaps = true, towns_industries = true }

local function ser(v, indent)
  indent = indent or ''
  local t = type(v)
  if t == 'string' then return string.format('%q', v) end
  if t == 'number' or t == 'boolean' then return tostring(v) end
  if t ~= 'table' then return 'nil' end
  local lines = { '{' }
  local n = #v
  for i = 1, n do lines[#lines + 1] = indent .. '\t' .. ser(v[i], indent .. '\t') .. ',' end
  local keys = {}
  for k in pairs(v) do
    if not (type(k) == 'number' and k >= 1 and k <= n and k % 1 == 0) then keys[#keys + 1] = k end
  end
  table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
  for _, k in ipairs(keys) do
    local key = type(k) == 'string' and k:match('^[%a_][%w_]*$') and k or ('[' .. ser(k) .. ']')
    lines[#lines + 1] = indent .. '\t' .. key .. ' = ' .. ser(v[k], indent .. '\t') .. ','
  end
  lines[#lines + 1] = indent .. '}'
  return table.concat(lines, '\n')
end

local app = {}
G.app_calls = { save = 0, load = 0, remove = 0 }
function app.getUserDataFolder() return USERDATA .. '/' end
function app.saveUserdata(dir, name, tbl)
  if not ALLOWED[dir] then error('saveUserdata failed: The directory you trying to access is not available or invalid') end
  G.app_calls.save = G.app_calls.save + 1
  local f = assert(io.open(USERDATA .. '/' .. dir .. '/' .. name .. '.lua', 'wb'))
  f:write('function data()\nreturn ' .. ser(tbl) .. '\nend\n')
  f:close()
end
function app.loadUserdata(dir, name)
  G.app_calls.load = G.app_calls.load + 1
  local f = io.open(USERDATA .. '/' .. dir .. '/' .. name .. '.lua', 'rb')
  if not f then error('loadUserdata: file not found ' .. name) end
  local src = f:read('*a')
  f:close()
  local env = {}
  local chunk = assert(load(src, '=' .. name, 't', env))
  chunk()
  return env.data()
end
function app.removeUserdata(dir, name)
  G.app_calls.remove = G.app_calls.remove + 1
  os.remove(USERDATA .. '/' .. dir .. '/' .. name .. '.lua')
end
G.saved_games = {}
function app.saveGame(name) G.saved_games[#G.saved_games + 1] = name end
G.app = app

return G

end
