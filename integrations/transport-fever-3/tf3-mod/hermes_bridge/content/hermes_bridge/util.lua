-- Small helpers shared by the Hermes Bridge modules (Lua 5.2, no globals).
--
-- Engine structs arrive as plain Lua tables or as userdata wrappers, and
-- several documented API paths are unverified on the live game. Everything
-- here degrades to nil instead of raising, so one API mismatch costs one
-- field, not the whole snapshot.
--
-- Never iterate a native (userdata) vector with ipairs/pairs: on TF3 that
-- can loop forever (reported by mod authors against build 40420). Use
-- util.list, which goes through :size()/:at() for userdata.

local util = {}

-- Call fn(...) and return its first result, or nil (plus the error) on failure.
function util.try(fn, ...)
  if type(fn) ~= 'function' then return nil, 'not a function' end
  local ok, res = pcall(fn, ...)
  if ok then return res end
  return nil, res
end

-- Resolve a dotted path below a root table without raising, e.g.
-- util.path(api, 'engine.util.getPlayer').
function util.path(root, dotted)
  local cur = root
  for part in string.gmatch(dotted, '[^%.]+') do
    if cur == nil then return nil end
    local ok, nxt = pcall(function() return cur[part] end)
    if not ok then return nil end
    cur = nxt
  end
  return cur
end

local MAX_LIST = 100000

-- Marks a table as a JSON array so it encodes as [] even when empty.
util.ARRAY_MT = { __jsontype = 'array' }

-- Return the elements of an engine list (Lua array or native vector) as a
-- fresh 1-based Lua array. Native vectors are read as v:at(1..v:size()),
-- the indexing working community mods use in the game.
function util.list(v)
  local out = setmetatable({}, util.ARRAY_MT)
  if v == nil then return out end
  if type(v) == 'table' then
    for i = 1, math.min(#v, MAX_LIST) do
      if v[i] ~= nil then out[#out + 1] = v[i] end
    end
    return out
  end
  if type(v) ~= 'userdata' then return out end
  local n = util.try(function() return v:size() end)
  if type(n) ~= 'number' then n = util.try(function() return #v end) end
  if type(n) ~= 'number' or n <= 0 then return out end
  for i = 1, math.min(n, MAX_LIST) do
    local item = util.try(function() return v:at(i) end)
    if item == nil then item = util.try(function() return v[i] end) end
    if item ~= nil then out[#out + 1] = item end
  end
  return out
end

-- Values of a Lua table or a native *map* (pairs works on engine maps; never
-- call this on a native vector).
function util.values(m)
  local out = setmetatable({}, util.ARRAY_MT)
  if m == nil then return out end
  pcall(function()
    for _, item in pairs(m) do
      out[#out + 1] = item
      if #out >= MAX_LIST then break end
    end
  end)
  return out
end

function util.vec3(v)
  if v == nil then return nil end
  local x = util.try(function() return v.x end)
  if x == nil then x = util.try(function() return v[1] end) end
  local y = util.try(function() return v.y end)
  if y == nil then y = util.try(function() return v[2] end) end
  local z = util.try(function() return v.z end)
  if z == nil then z = util.try(function() return v[3] end) end
  if type(x) ~= 'number' or type(y) ~= 'number' then return nil end
  return { util.round(x, 1), util.round(y, 1), util.round(type(z) == 'number' and z or 0, 1) }
end

function util.round(n, digits)
  if type(n) ~= 'number' then return n end
  local m = 10 ^ (digits or 0)
  return math.floor(n * m + 0.5) / m
end

-- Name of an api.type.enum.<enum_type> value, looked up through the known
-- member names (enums cannot be listed with pairs on this game build).
local enum_maps = {}
function util.enum_name(enum_type, names, value)
  if value == nil then return nil end
  local map = enum_maps[enum_type]
  if map == nil then
    map = { _list = {} }
    local enum = util.path(api, 'type.enum.' .. enum_type)
    for _, name in ipairs(names) do
      local v = enum ~= nil and util.try(function() return enum[name] end) or nil
      if v ~= nil then
        map[v] = name
        map._list[#map._list + 1] = { v, name }
      end
    end
    enum_maps[enum_type] = map
  end
  if map[value] ~= nil then return map[value] end
  for _, pair in ipairs(map._list) do
    if util.try(function() return pair[1] == value end) then return pair[2] end
  end
  if type(value) == 'number' or type(value) == 'string' then return value end
  return util.try(tostring, value)
end

-- Bounded conversion of an arbitrary Lua/engine value into something
-- json.encode can serialise (used by lua_eval and get_entity). Userdata is
-- read through :size()/:at() when it looks like a vector, otherwise through
-- tostring; it is never iterated with pairs.
function util.plain(v, depth, budget)
  depth = depth or 0
  budget = budget or { n = 2000 }
  budget.n = budget.n - 1
  if budget.n < 0 then return '<truncated>' end
  local tv = type(v)
  if tv == 'nil' or tv == 'boolean' or tv == 'number' or tv == 'string' then return v end
  if depth >= 5 then return '<' .. tv .. '>' end
  if tv == 'userdata' then
    if type(util.try(function() return v:size() end)) == 'number' then
      local out = {}
      for _, item in ipairs(util.list(v)) do
        out[#out + 1] = util.plain(item, depth + 1, budget)
        if budget.n < 0 then break end
      end
      return out
    end
    return util.try(tostring, v) or '<userdata>'
  end
  if tv ~= 'table' then return '<' .. tv .. '>' end
  local out = {}
  for k, item in pairs(v) do
    if type(k) == 'string' or type(k) == 'number' then
      out[k] = util.plain(item, depth + 1, budget)
    end
    if budget.n < 0 then break end
  end
  return out
end

function util.clamp_int(v, lo, hi, default)
  local n = tonumber(v)
  if n == nil then return default end
  n = math.floor(n)
  if n < lo then return lo end
  if n > hi then return hi end
  return n
end

-- Wall-clock seconds (mods get os.time/clock/date/getenv), or nil without them.
function util.wall_time()
  local t = util.try(util.path(os, 'time'))
  if type(t) == 'number' then return t end
  return nil
end

-- Seconds for coarse timeouts; the frame counter is a fallback so timeouts
-- still progress without a wall clock. Never use it for engine deadlines,
-- which the GUI and the simulation must read off the same clock.
function util.now(frame)
  return util.wall_time() or (frame or 0) / 60
end

return util
