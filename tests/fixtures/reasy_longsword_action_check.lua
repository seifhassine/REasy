-- Read-only runtime check for the new Action in LongSword.motfsm2.43.
-- BHVT access: https://github.com/lingsamuel/RE-BHVT-Editor
local TARGET_ID = 0x7C152E99
local TARGET_TYPE = "snow.player.fsm.PlayerFsm2ActionDogRideAccess"
local REPORT_PATH = "reasy_longsword_action_check.json"
local get_id = sdk.find_type_definition("via.behaviortree.Action"):get_method("get_ID")
local fsm_type = sdk.typeof("via.motion.MotionFsm2")
local pending = true
local next_check = 0
local status = "Waiting for the local player..."
local report = nil

assert(get_id, "via.behaviortree.Action.get_ID is unavailable")

local function describe(action)
    return {
        id = string.format("0x%08X", get_id:call(action)),
        type = action:get_type_definition():get_full_name(),
        address = string.format("0x%X", action:get_address()),
        enabled = action:call("get_Enabled"),
    }
end

local function scan_runtime()
    local manager = sdk.get_managed_singleton("snow.player.PlayerManager")
    local player = manager and manager:call("findMasterPlayer")
    if not player then return nil end
    local game_object = player:call("get_GameObject")
    local fsm = game_object and game_object:call("getComponent(System.Type)", fsm_type)
    if not fsm then return nil end

    local result = {
        checked_at = os.date("%Y-%m-%d %H:%M:%S"),
        target_id = string.format("0x%08X", TARGET_ID),
        expected_type = TARGET_TYPE,
        player_address = string.format("0x%X", player:get_address()),
        loaded_and_referenced = false,
        layers = {},
    }
    for layer_index = 0, fsm:call("getLayerCount") - 1 do
        local layer = fsm:call("getLayer", layer_index)
        local tree = layer and layer:get_tree_object()
        if tree then
            local entry = {
                layer = layer_index,
                action_count = tree:get_action_count(),
                node_count = tree:get_node_count(),
                matches = {},
                wait_actions = {},
                loaded_and_referenced = false,
            }
            for index = 0, entry.action_count - 1 do
                local action = tree:get_action(index)
                if action and get_id:call(action) == TARGET_ID then
                    local match = describe(action)
                    match.runtime_action_index = index
                    table.insert(entry.matches, match)
                end
            end
            -- Node 1 is the edited "wait" node; verify its name before inspecting it.
            local node = entry.node_count > 1 and tree:get_node(1)
            if node and node:get_name() == "wait" then
                entry.wait_node_name = node:get_full_name()
                for index, action in ipairs(node:get_actions()) do
                    local item = describe(action)
                    item.reference_index = index - 1
                    table.insert(entry.wait_actions, item)
                    if get_id:call(action) == TARGET_ID and item.type == TARGET_TYPE then
                        for _, match in ipairs(entry.matches) do
                            if match.address == item.address then
                                entry.loaded_and_referenced = true
                                result.loaded_and_referenced = true
                            end
                        end
                    end
                end
            end
            table.insert(result.layers, entry)
        end
    end
    if #result.layers == 0 then return nil end
    return result
end

re.on_pre_application_entry("UpdateScene", function()
    if not pending or os.clock() < next_check then return end
    next_check = os.clock() + 2
    -- Engine objects may disappear while changing scenes; report boundary errors.
    local ok, result = pcall(scan_runtime)
    if not ok then
        pending = false
        status = "ERROR: " .. tostring(result)
        report = { error = tostring(result), target_id = string.format("0x%08X", TARGET_ID) }
    elseif result == nil then
        return
    else
        pending = false
        report = result
        status = result.loaded_and_referenced
            and "FOUND: new Action is loaded and referenced by wait."
            or "NOT CONFIRMED: inspect layer results below."
    end
    log.info("[REasy Action Check] " .. status)
    json.dump_file(REPORT_PATH, report)
end)

re.on_draw_ui(function()
    if not imgui.tree_node("REasy LongSword Action Check") then return end
    imgui.text(status)
    if imgui.button("Check again") then
        pending = true
        next_check = 0
        report = nil
        status = "Checking on the next UpdateScene..."
    end
    if report and report.layers then
        for _, layer in ipairs(report.layers) do
            imgui.text(string.format("Layer %d: %d actions, %d nodes; target matches: %d",
                layer.layer, layer.action_count, layer.node_count, #layer.matches))
            for _, match in ipairs(layer.matches) do
                imgui.text(string.format("  %s [%d] %s", match.id, match.runtime_action_index, match.address))
                imgui.text("  " .. match.type)
            end
            if layer.wait_node_name then
                imgui.text(string.format("  %s: %d references; new Action linked: %s",
                    layer.wait_node_name, #layer.wait_actions, tostring(layer.loaded_and_referenced)))
            end
        end
    end
    imgui.text("Report: reframework/data/" .. REPORT_PATH)
    imgui.tree_pop()
end)
