local core = require("apisix.core")

local COOKIE_HEADER             = "Cookie"
local OKAPI_TOKEN_HEADER        = "X-Okapi-Token"
local AUTHORIZATION_HEADER      = "Authorization"
local FOLIO_ACCESS_TOKEN_COOKIE = "folioAccessToken"

local schema = {
    type = "object",
    properties = {
        set_okapi_header = {
            type    = "boolean",
            default = true,
        },
        set_authorization_header = {
            type    = "boolean",
            default = false,
        },
        clean_access_token_cookie = {
            type    = "boolean",
            default = false,
        },
    },
}

local _M = {
    version  = 1.0,
    priority = 1010,
    name     = "auth-headers-manager",
    schema   = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

local function get_cookies()
    local cookie_header_value = ngx.req.get_headers()[COOKIE_HEADER]
    if not cookie_header_value then
        return {}
    end

    local cookies = {}
    local iterator, err = ngx.re.gmatch(cookie_header_value, "([^\\s]+)=([^\\s;]+)[;\\s]*", "io")
    if not iterator or err then
        return {}
    end

    while true do
        local m, match_err = iterator()
        if match_err then
            return {}
        end
        if not m then
            break
        end
        cookies[m[1]] = m[2]
    end

    return cookies
end

local function get_cookie_header_without_access_token(cookies)
    if cookies == nil then
        return ""
    end

    local result_table = {}
    for key, value in pairs(cookies) do
        if key == FOLIO_ACCESS_TOKEN_COOKIE then
            goto continue
        end
        table.insert(result_table, key .. "=" .. value)
        ::continue::
    end

    return table.concat(result_table, ";")
end

local function starts_with(str, start)
    return str:sub(1, #start) == start
end

local function get_access_token_from_headers()
    local headers             = ngx.req.get_headers()
    local okapi_auth_token    = headers[OKAPI_TOKEN_HEADER]
    local authorization_token = headers[AUTHORIZATION_HEADER]

    if authorization_token then
        if not starts_with(authorization_token, "Bearer ") then
            core.response.exit(404,
                "Invalid authorization header, value must start with Bearer")
            return
        end

        local authorization_token_value = authorization_token:sub(8, authorization_token:len())
        if okapi_auth_token and okapi_auth_token ~= authorization_token_value then
            core.response.exit(404, "X-Okapi-Token is not equal to Authorization token")
            return
        end

        return { source = AUTHORIZATION_HEADER, token = authorization_token_value }
    end

    return { source = OKAPI_TOKEN_HEADER, token = okapi_auth_token }
end

local function get_access_token(cookies)
    local folio_access_token = cookies[FOLIO_ACCESS_TOKEN_COOKIE]
    local headers_token      = get_access_token_from_headers()
    if not headers_token then
        return
    end

    if folio_access_token then
        if headers_token.token and headers_token.token ~= folio_access_token then
            core.response.exit(404,
                headers_token.source .. " token is not equal to " ..
                FOLIO_ACCESS_TOKEN_COOKIE .. " token in cookies")
            return
        end
        return { source = FOLIO_ACCESS_TOKEN_COOKIE, token = folio_access_token }
    end

    return headers_token
end

function _M.rewrite(conf, ctx)
    local cookies      = get_cookies()
    local access_token = get_access_token(cookies)
    if not access_token then
        return
    end

    core.log.debug("is Okapi token enabled: ", conf.set_okapi_header)
    if conf.set_okapi_header then
        if access_token.source == FOLIO_ACCESS_TOKEN_COOKIE
            and not ngx.req.get_headers()[OKAPI_TOKEN_HEADER]
        then
            core.log.debug("Setting X-Okapi-Token header from cookie value")
            ngx.req.clear_header(AUTHORIZATION_HEADER)
            ngx.req.set_header(OKAPI_TOKEN_HEADER, access_token.token)
        end
    end

    core.log.debug("is Authorization token enabled: ", conf.set_authorization_header)
    if conf.set_authorization_header then
        if access_token.source == FOLIO_ACCESS_TOKEN_COOKIE
            and not ngx.req.get_headers()[AUTHORIZATION_HEADER]
        then
            core.log.debug("Setting Authorization header from cookie value")
            ngx.req.clear_header(OKAPI_TOKEN_HEADER)
            ngx.req.set_header(AUTHORIZATION_HEADER, "Bearer " .. access_token.token)
        end
    end

    core.log.debug("is clean access token cookie enabled: ", conf.clean_access_token_cookie)
    if conf.clean_access_token_cookie then
        ngx.req.clear_header(COOKIE_HEADER)
        local new_cookie_header_value = get_cookie_header_without_access_token(cookies)
        if new_cookie_header_value ~= "" then
            ngx.req.set_header(COOKIE_HEADER, new_cookie_header_value)
        end
    end
end

return _M
