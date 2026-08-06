local core = require("apisix.core")
local cjson = require("cjson")

local _M = {
    version  = 1.0,
    priority = 500,
    name     = "version-info",
    schema   = { type = "object" },
}

function _M.check_schema(conf)
    return core.schema.check(_M.schema, conf)
end

function _M.access(conf, ctx)
    local ver = (core.version and core.version.VERSION) or "unknown"

    core.response.set_header("Content-Type",              "application/json")
    core.response.set_header("Cache-Control",             "private, no-cache, no-store, max-age=0")
    core.response.set_header("Pragma",                    "no-cache")
    core.response.set_header("Expires",                   "0")
    core.response.set_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")

    core.response.exit(200, cjson.encode({ version = ver }))
end

return _M
