-- Read-only game state for the Hermes Bridge (runs in the GUI state, where
-- api.engine is a read-only view of the whole entity component system).
--
-- API paths follow the official script reference
-- (https://wiki.transportfever3.com/script-doc/api/engine.html and
-- .../engine/system.html, .../engine/util.html). Each section is computed
-- under pcall: if one path differs on the live build, that section reports
-- an error string and the rest of the snapshot still arrives.
--
-- Loaded by hermes_bridge.script.lua as a factory: snapshot(util) -> module.

return function(util)

local M = {}

local function CT(name)
  return util.path(api, 'type.ComponentType.' .. name)
end

-- getComponent on an entity that no longer exists is a fatal engine
-- assertion that pcall cannot catch (reported against build 40420), and ids
-- are reused, so every read checks entityExists first.
local function alive(entity)
  if type(entity) ~= 'number' then return false end
  local exists = util.path(api, 'engine.entityExists')
  if exists == nil then return true end
  return util.try(exists, entity) == true
end

local function component(entity, type_name)
  local ct = CT(type_name)
  if ct == nil or not alive(entity) then return nil end
  return util.try(api.engine.getComponent, entity, ct)
end

local function name_of(entity)
  if not alive(entity) then return nil end
  local n = util.try(util.path(api, 'engine.util.getEntityName'), entity)
  if n == nil then
    local comp = component(entity, 'NAME')
    n = comp and util.try(function() return comp.name end)
  end
  return n
end

function M.player()
  return util.try(util.path(api, 'engine.util.getPlayer'))
end

local function entities_with(type_name, player)
  local ct = CT(type_name)
  local out = {}
  if ct == nil then return out end
  if player ~= nil then
    local owned = util.try(util.path(api, 'engine.getEntitiesWithComponent'), ct, { requireOwnedByPlayer = player })
    if owned ~= nil then return util.list(owned) end
  end
  local each = util.path(api, 'engine.forEachEntityWithComponent')
  if each then
    -- Documented order: callback first, component type second.
    local ok = pcall(each, function(e) out[#out + 1] = e end, ct)
    if ok then return out end
    out = {}
  end
  local list = util.try(util.path(api, 'engine.getEntitiesWithComponent'), ct)
  return util.list(list)
end

local function owned_by(entity, player)
  if player == nil then return true end
  local po = component(entity, 'PLAYER_OWNED')
  if po == nil then return false end
  return util.try(function() return po.player end) == player
end

local function construction_position(construction_entity)
  local con = component(construction_entity, 'CONSTRUCTION')
  if con == nil then return nil end
  local transf = util.try(function() return con.transf end)
  if transf == nil then return nil end
  local getTransl = util.path(api, 'type.getTransl') or util.path(api, 'type.Mat4f.getTransl')
  local pos = getTransl and util.try(getTransl, transf)
  if pos then return util.vec3(pos) end
  -- Fallback: column-major 4x4, translation in elements 13..15.
  local x = util.try(function() return transf[13] end)
  local y = util.try(function() return transf[14] end)
  local z = util.try(function() return transf[15] end)
  if type(x) == 'number' and type(y) == 'number' then
    return { util.round(x, 1), util.round(y, 1), util.round(z or 0, 1) }
  end
  return nil
end

local function world_component(type_name)
  local world = util.try(util.path(api, 'engine.util.getWorld'))
  local comp = world and component(world, type_name)
  if comp then return comp end
  local list = entities_with(type_name)
  return list[1] and component(list[1], type_name) or nil
end

-- Sections ------------------------------------------------------------------

local sections = {}
local line_balance

function sections.game(ctx)
  local gt = world_component('GAME_TIME')
  local gs = world_component('GAME_SPEED')
  local game_time = gt and util.try(function() return gt.gameTime end)
  local date
  local d = game_time and util.try(util.path(api, 'engine.util.getCalendarDate'), game_time)
  if d then
    date = {
      year = util.try(function() return d.year end),
      month = util.try(function() return d.month end),
      day = util.try(function() return d.day end),
    }
  end
  return {
    game_time = game_time,
    date = date,
    year = util.try(util.path(api, 'engine.util.getYear')),
    speedup = gs and util.try(function() return gs.speedup end),
    paused = gs and util.try(function() return gs.speedup == 0 end),
    millis_per_day = gs and util.try(function() return gs.millisPerDay end),
  }
end

function sections.finance(ctx)
  local player = ctx.player
  local acc = component(player, 'ACCOUNT')
  local balance = util.try(util.path(api, 'engine.util.finance.getPlayersBalance'), player)
  if balance == nil and acc then balance = util.try(function() return acc.balance end) end
  return {
    balance = balance,
    loan = acc and util.try(function() return acc.loan end),
    earnings = util.try(util.path(api, 'engine.util.finance.calculateEarnings'), player),
  }
end

local function line_stops(line_comp)
  local stops = {}
  for _, stop in ipairs(util.list(line_comp and util.try(function() return line_comp.stops end))) do
    stops[#stops + 1] = {
      station_group_id = util.try(function() return stop.stationGroup end),
      station_index = util.try(function() return stop.station end),
      terminal = util.try(function() return stop.terminal end),
    }
  end
  return stops
end

-- Best effort: per-entity balance over the last in-game year. The call
-- shape is taken from working community mods; units are game ticks.
function line_balance(entity, ctx)
  local calc = util.path(api, 'engine.util.finance.calculateBalance')
  if calc == nil or ctx.game_time == nil then return nil end
  local year = ctx.year_ticks
  if year == nil then return nil end
  return util.try(calc, { entity }, math.max(0, ctx.game_time - year), ctx.game_time, true)
end

function sections.lines(ctx)
  local ls = util.path(api, 'engine.system.lineSystem')
  local tvs = util.path(api, 'engine.system.transportVehicleSystem')
  local ids = util.list(ls and util.try(ls.getLinesForPlayer, ctx.player))
  local out = { count = #ids, items = {} }
  for i, line in ipairs(ids) do
    if i > ctx.limit then out.truncated = true break end
    local comp = component(line, 'LINE')
    local vehicles = util.list(tvs and util.try(tvs.getLineVehicles, line))
    local stops = line_stops(comp)
    local modes = {}
    local tm = comp and util.try(function() return comp.vehicleInfo.transportModes end)
    if type(tm) == 'table' then
      for mode, on in pairs(tm) do if on then modes[#modes + 1] = mode end end
    end
    local usage = util.try(util.path(api, 'engine.util.line.getLineCapacityUsages'), line, false)
    local used, capacity = 0, 0
    for _, u in ipairs(util.list(usage)) do
      used = used + (tonumber(util.try(function() return u.used end)) or 0)
      capacity = capacity + (tonumber(util.try(function() return u.capacity end)) or 0)
    end
    out.items[#out.items + 1] = {
      id = line,
      name = name_of(line),
      stops = stops,
      vehicle_count = #vehicles,
      vehicle_ids = vehicles,
      transport_modes = modes,
      load = capacity > 0 and { used = used, capacity = capacity, ratio = util.round(used / capacity, 2) } or nil,
      balance_last_year = line_balance(line, ctx),
    }
  end
  return out
end

local function vehicle_record(v)
  local tv = component(v, 'TRANSPORT_VEHICLE')
  local enum = util.path(api, 'type.enum')
  local state = tv and util.try(function() return tv.state end)
  local carrier = tv and util.try(function() return tv.carrier end)
  local vutil = util.path(api, 'engine.util.vehicle')
  return {
    id = v,
    name = name_of(v),
    line_id = tv and util.try(function() return tv.line end),
    depot_id = tv and util.try(function() return tv.depot end),
    state = util.enum_name(enum and enum.TransportVehicleState, state),
    carrier = util.enum_name(enum and enum.Carrier, carrier),
    user_stopped = tv and util.try(function() return tv.userStopped end),
    no_path = tv and util.try(function() return tv.noPath end),
    stop_index = tv and util.try(function() return tv.stopIndex end),
    position = vutil and util.vec3(util.try(vutil.getPosition, v)),
    running_cost_per_year = vutil and util.try(vutil.getRunningCost, v),
    value = vutil and util.try(vutil.getDepreciatedValue, v),
  }
end

function sections.vehicles(ctx)
  local vutil = util.path(api, 'engine.util.vehicle')
  local ids = util.list(vutil and util.try(vutil.getVehicles))
  local out = { count = 0, items = {} }
  for _, v in ipairs(ids) do
    if out.count >= 5000 then out.truncated = true break end
    if owned_by(v, ctx.player) then
      out.count = out.count + 1
      if #out.items < ctx.limit then
        out.items[#out.items + 1] = vehicle_record(v)
      else
        out.truncated = true
      end
    end
  end
  return out
end

function sections.stations(ctx)
  local sgs = util.path(api, 'engine.system.stationGroupSystem')
  local ss = util.path(api, 'engine.system.stationSystem')
  local scs = util.path(api, 'engine.system.streetConnectorSystem')
  local ls = util.path(api, 'engine.system.lineSystem')
  local usage = util.path(api, 'engine.util.calculateStationUsage')
  local out = { count = 0, items = {} }
  for _, st in ipairs(entities_with('STATION', ctx.player)) do
    if owned_by(st, ctx.player) then
      out.count = out.count + 1
      if #out.items >= ctx.limit then
        out.truncated = true
      else
        local group = sgs and util.try(sgs.getStationGroup, st)
        local index
        local group_comp = group and component(group, 'STATION_GROUP')
        for i, member in ipairs(util.list(group_comp and util.try(function() return group_comp.stations end))) do
          if member == st then index = i - 1 end -- line stops use a 0-based index (INFERRED, see selftest)
        end
        local con = scs and util.try(scs.getConstructionEntityForStation, st)
        local u = usage and util.try(usage, st)
        out.items[#out.items + 1] = {
          id = st,
          name = name_of(st),
          station_group_id = group,
          index_in_group = index,
          town_id = ss and util.try(ss.getTown, st),
          construction_id = con,
          position = con and construction_position(con),
          line_ids = util.list(group and ls and util.try(ls.getLinesForStationGroup, group)),
          usage = u and {
            total_used = util.try(function() return u.totalUsed end),
            pool_capacity = util.try(function() return u.poolCapacity end),
          },
        }
      end
    end
  end
  return out
end

function sections.depots(ctx)
  local tvs = util.path(api, 'engine.system.transportVehicleSystem')
  local out = { count = 0, items = {} }
  for _, depot in ipairs(entities_with('VEHICLE_DEPOT', ctx.player)) do
    if owned_by(depot, ctx.player) then
      out.count = out.count + 1
      if #out.items >= ctx.limit then
        out.truncated = true
      else
        out.items[#out.items + 1] = {
          id = depot,
          name = name_of(depot),
          position = construction_position(depot),
          vehicle_ids = util.list(tvs and util.try(tvs.getDepotVehicles, depot)),
        }
      end
    end
  end
  return out
end

function sections.towns(ctx)
  local tutil = util.path(api, 'engine.util.town')
  local ss = util.path(api, 'engine.system.stationSystem')
  local out = { count = 0, items = {} }
  local towns = entities_with('TOWN')
  out.count = #towns
  for i, town in ipairs(towns) do
    if i > ctx.limit then out.truncated = true break end
    local usage = tutil and util.try(tutil.getTownCapacityUsage, town)
    local cap = {}
    for idx, entry in ipairs(util.list(usage)) do
      cap[#cap + 1] = {
        land_use = ({ 'residential', 'commercial', 'industrial' })[idx] or idx,
        used = util.try(function() return entry.used end),
        capacity = util.try(function() return entry.capacity end),
      }
    end
    out.items[#out.items + 1] = {
      id = town,
      name = name_of(town),
      capacity_usage = cap,
      station_ids = util.list(ss and util.try(ss.getStations, town)),
    }
  end
  return out
end

function sections.industries(ctx)
  local stock = util.path(api, 'engine.util.stock')
  local out = { count = 0, items = {} }
  local list = entities_with('INDUSTRY')
  out.count = #list
  for i, ind in ipairs(list) do
    if i > ctx.limit then out.truncated = true break end
    local comp = component(ind, 'INDUSTRY')
    local con = comp and util.try(function() return comp.construction end)
    local con_comp = con and component(con, 'CONSTRUCTION')
    local stock_list = comp and util.try(function() return comp.stockList end)
    out.items[#out.items + 1] = {
      id = ind,
      name = name_of(ind),
      type = con_comp and util.try(function() return con_comp.fileName end),
      level = comp and util.try(function() return comp.level end),
      max_level = comp and util.try(function() return comp.maxLevel end),
      position = con and construction_position(con),
      shipped_per_year = stock and stock_list and util.try(stock.getCargoShippedPerYear, stock_list),
      production_rating = stock and stock_list and util.try(stock.getProductionRating, stock_list),
    }
  end
  return out
end

function sections.problems(ctx)
  local ls = util.path(api, 'engine.system.lineSystem')
  local tvs = util.path(api, 'engine.system.transportVehicleSystem')
  local lines = {}
  for _, pair in ipairs(util.list(ls and util.try(ls.getProblemLines, ctx.player))) do
    local line = util.try(function() return pair[1] end)
    lines[#lines + 1] = {
      line_id = line,
      name = line and name_of(line),
      problem = util.plain(util.try(function() return pair[2] end)),
    }
  end
  local no_path = util.list(tvs and util.try(tvs.getNoPathVehicles))
  return { problem_lines = lines, no_path_vehicle_ids = no_path }
end

M.SECTIONS = sections

function M.snapshot(args)
  local ctx = {
    player = M.player(),
    limit = util.clamp_int(args and args.limit, 1, 200, 50),
  }
  local gt = world_component('GAME_TIME')
  local gs = world_component('GAME_SPEED')
  ctx.game_time = gt and util.try(function() return gt.gameTime end)
  local mpd = gs and util.try(function() return gs.millisPerDay end)
  ctx.year_ticks = mpd and mpd > 0 and util.try(util.path(api, 'util.getYearDuration'), mpd) or nil
  local wanted = (args and args.sections) or { 'game', 'finance', 'lines', 'problems' }
  local out = { player_id = ctx.player }
  for _, name in ipairs(wanted) do
    local fn = sections[name]
    if fn == nil then
      out[name] = { error = 'unknown section' }
    else
      local ok, res = pcall(fn, ctx)
      out[name] = ok and res or { error = tostring(res) }
    end
  end
  return out
end

function M.entity(id)
  if not alive(id) then return { id = id, exists = false } end
  local out = { id = id, exists = true, name = name_of(id), components = {} }
  for _, type_name in ipairs({ 'LINE', 'TRANSPORT_VEHICLE', 'STATION', 'STATION_GROUP', 'TOWN', 'INDUSTRY',
                               'CONSTRUCTION', 'VEHICLE_DEPOT', 'PLAYER_OWNED', 'ACCOUNT' }) do
    local comp = component(id, type_name)
    if comp ~= nil then out.components[type_name] = util.plain(comp) end
  end
  local con = component(id, 'CONSTRUCTION')
  if con then out.position = construction_position(id) end
  return out
end

M.component = component
M.alive = alive
M.name_of = name_of
M.construction_position = construction_position
M.world_component = world_component

return M

end
