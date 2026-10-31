-- Minimal JSON encoder/decoder for the Hermes Bridge mod (Lua 5.2, no globals).
--
-- encode(value) -> string. Tables with keys 1..n (and nothing else) become
-- arrays; every other table becomes an object with string keys. An empty
-- table encodes as {} unless its metatable has __jsontype = 'array' (as
-- json.array() and util.list() tables do; decoded empty arrays round-trip).
-- NaN/inf become null.
-- Cycles raise an error instead of recursing forever.
--
-- decode(text) -> value. Strict RFC 8259 parser. An object member whose value
-- is null is dropped (models often send null for "not given", and every
-- caller here treats absent and null alike); null inside an array becomes the
-- json.NULL sentinel so array lengths stay right.

local json = {}

json.NULL = setmetatable({}, { __tostring = function() return "null" end })
local ARRAY_MT = { __jsontype = 'array' }

-- A table that encodes as a JSON array even while empty.
function json.array(t)
  return setmetatable(t or {}, ARRAY_MT)
end

local MAX_DEPTH = 64

local escapes = {
  ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function escape_string(s)
  return '"' .. s:gsub('[%c"\\]', function(c)
    return escapes[c] or string.format('\\u%04x', c:byte())
  end) .. '"'
end

local function is_array(t)
  local n = 0
  for k in pairs(t) do
    if type(k) ~= 'number' or k <= 0 or k % 1 ~= 0 then
      return false, 0
    end
    if k > n then n = k end
  end
  for i = 1, n do
    if t[i] == nil then return false, 0 end
  end
  return true, n
end

local function encode_number(v)
  if v ~= v or v == math.huge or v == -math.huge then
    return 'null'
  end
  if v % 1 == 0 and v >= -2^53 and v <= 2^53 then
    return string.format('%d', v)
  end
  return string.format('%.14g', v)
end

local encode_value

local function encode_table(t, seen, depth)
  if seen[t] then error('json.encode: cycle detected') end
  if depth > MAX_DEPTH then error('json.encode: nesting too deep') end
  if t == json.NULL then return 'null' end
  local mt = getmetatable(t)
  if mt and mt.__jsontype == 'array' and next(t) == nil then return '[]' end
  seen[t] = true
  local out = {}
  local array, n = is_array(t)
  if array and n > 0 then
    for i = 1, n do
      out[i] = encode_value(t[i], seen, depth + 1)
    end
    seen[t] = nil
    return '[' .. table.concat(out, ',') .. ']'
  end
  local keys = {}
  for k in pairs(t) do
    keys[#keys + 1] = k
  end
  table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
  for _, k in ipairs(keys) do
    local v = t[k]
    local tv = type(v)
    if tv ~= 'function' and tv ~= 'userdata' and tv ~= 'thread' then
      out[#out + 1] = escape_string(tostring(k)) .. ':' .. encode_value(v, seen, depth + 1)
    end
  end
  seen[t] = nil
  return '{' .. table.concat(out, ',') .. '}'
end

encode_value = function(v, seen, depth)
  local tv = type(v)
  if tv == 'nil' then return 'null' end
  if tv == 'boolean' then return v and 'true' or 'false' end
  if tv == 'number' then return encode_number(v) end
  if tv == 'string' then return escape_string(v) end
  if tv == 'table' then return encode_table(v, seen, depth) end
  -- userdata (engine structs), functions, threads: describe, don't fail.
  return escape_string('<' .. tv .. '>')
end

function json.encode(v)
  return encode_value(v, {}, 0)
end

-- Decoder -------------------------------------------------------------------

local function decode_error(str, pos, msg)
  error(string.format('json.decode: %s at position %d', msg, pos), 0)
end

local function skip_ws(str, pos)
  return str:find('[^ \t\r\n]', pos) or (#str + 1)
end

local function utf8_char(cp)
  if cp < 0x80 then return string.char(cp) end
  if cp < 0x800 then
    return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
  end
  if cp < 0x10000 then
    return string.char(0xE0 + math.floor(cp / 0x1000), 0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
  end
  return string.char(0xF0 + math.floor(cp / 0x40000), 0x80 + math.floor(cp / 0x1000) % 0x40,
    0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
end

local simple_escapes = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' }

local function parse_string(str, pos)
  local buf = {}
  local i = pos + 1
  while true do
    local c = str:sub(i, i)
    if c == '' then decode_error(str, i, 'unterminated string') end
    if c == '"' then
      return table.concat(buf), i + 1
    end
    if c == '\\' then
      local e = str:sub(i + 1, i + 1)
      if simple_escapes[e] then
        buf[#buf + 1] = simple_escapes[e]
        i = i + 2
      elseif e == 'u' then
        local hex = str:sub(i + 2, i + 5)
        if not hex:match('^%x%x%x%x$') then decode_error(str, i, 'bad \\u escape') end
        local cp = tonumber(hex, 16)
        i = i + 6
        if cp >= 0xD800 and cp <= 0xDBFF then
          local lo = str:match('^\\u(%x%x%x%x)', i)
          local lo_cp = lo and tonumber(lo, 16)
          if lo_cp and lo_cp >= 0xDC00 and lo_cp <= 0xDFFF then
            cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo_cp - 0xDC00)
            i = i + 6
          else
            cp = 0xFFFD
          end
        elseif cp >= 0xDC00 and cp <= 0xDFFF then
          cp = 0xFFFD
        end
        buf[#buf + 1] = utf8_char(cp)
      else
        decode_error(str, i, 'bad escape')
      end
    elseif c:byte() < 0x20 then
      decode_error(str, i, 'control character in string')
    else
      local j = str:find('["\\%c]', i)
      if not j then decode_error(str, i, 'unterminated string') end
      buf[#buf + 1] = str:sub(i, j - 1)
      i = j
    end
  end
end

local parse_value

local function parse_array(str, pos, depth)
  local arr = json.array()
  local i = skip_ws(str, pos + 1)
  if str:sub(i, i) == ']' then return arr, i + 1 end
  local n = 0
  while true do
    local v
    v, i = parse_value(str, i, depth + 1)
    n = n + 1
    arr[n] = v
    i = skip_ws(str, i)
    local c = str:sub(i, i)
    if c == ']' then return arr, i + 1 end
    if c ~= ',' then decode_error(str, i, "expected ',' or ']'") end
    i = skip_ws(str, i + 1)
  end
end

local function parse_object(str, pos, depth)
  local obj = {}
  local i = skip_ws(str, pos + 1)
  if str:sub(i, i) == '}' then return obj, i + 1 end
  while true do
    if str:sub(i, i) ~= '"' then decode_error(str, i, 'expected string key') end
    local k
    k, i = parse_string(str, i)
    i = skip_ws(str, i)
    if str:sub(i, i) ~= ':' then decode_error(str, i, "expected ':'") end
    local v
    v, i = parse_value(str, skip_ws(str, i + 1), depth + 1)
    if v ~= json.NULL then obj[k] = v end
    i = skip_ws(str, i)
    local c = str:sub(i, i)
    if c == '}' then return obj, i + 1 end
    if c ~= ',' then decode_error(str, i, "expected ',' or '}'") end
    i = skip_ws(str, i + 1)
  end
end

parse_value = function(str, pos, depth)
  if depth > MAX_DEPTH then decode_error(str, pos, 'nesting too deep') end
  local i = skip_ws(str, pos)
  local c = str:sub(i, i)
  if c == '{' then return parse_object(str, i, depth) end
  if c == '[' then return parse_array(str, i, depth) end
  if c == '"' then return parse_string(str, i) end
  if str:sub(i, i + 3) == 'true' then return true, i + 4 end
  if str:sub(i, i + 4) == 'false' then return false, i + 5 end
  if str:sub(i, i + 3) == 'null' then return json.NULL, i + 4 end
  local num = str:match('^-?%d+%.?%d*[eE]?[-+]?%d*', i)
  if num and num ~= '' and num ~= '-' then
    local value = tonumber(num)
    if value == nil then decode_error(str, i, 'bad number') end
    return value, i + #num
  end
  decode_error(str, i, 'unexpected character')
end

function json.decode(str)
  if type(str) ~= 'string' then error('json.decode: expected string', 0) end
  local value, pos = parse_value(str, 1, 0)
  pos = skip_ws(str, pos)
  if pos <= #str then decode_error(str, pos, 'trailing data') end
  return value
end

return json
