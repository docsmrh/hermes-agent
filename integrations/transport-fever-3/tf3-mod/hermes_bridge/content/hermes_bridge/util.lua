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
-- fresh 1-based Lua array.
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
  n = math.min(n, MAX_LIST)
  -- Native vectors are 0-based through :at(); fall back to 1-based indexing.
  local first = util.try(function() return v:at(0) end)
  if first ~= nil then
    for i = 0, n - 1 do
      local item = util.try(function() return v:at(i) end)
      if item ~= nil then out[#out + 1] = item end
    end
    return out
  end
  for i = 1, n do
    local item = util.try(function() return v[i] end)
    if item ~= nil then out[#out + 1] = item end
  end
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

-- Map an enum value back to its name when the enum is a plain Lua table.
function util.enum_name(enum_table, value)
  if type(enum_table) ~= 'table' or value == nil then return value end
  for k, v in pairs(enum_table) do
    if v == value then return k end
  end
  return value
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

-- Wall-clock seconds for coarse timeouts (mods get os.time/clock/date/getenv);
-- the frame counter is a fallback so timeouts still progress without it.
function util.now(frame)
  local t = util.try(util.path(os, 'time'))
  if type(t) == 'number' then return t end
  return (frame or 0) / 60
end

return util
