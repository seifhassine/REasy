-- Read-only check of the second LongSword FSM test fixture.
local ACTION_ID = 0x7C152E99
local CONDITION_ID = 0x433DD98A
local NODE_ID = 0xA0998E05
local ACTION_INDEX, CONDITION_INDEX, NODE_INDEX = 9785, 7165, 4478
local REPORT_PATH = "reasy_fsm_structure_check.json"
local action_id = sdk.find_type_definition("via.behaviortree.Action"):get_method("get_ID")
local condition_id = sdk.find_type_definition("via.behaviortree.Condition"):get_method("get_ID")
local fsm_type = sdk.typeof("via.motion.MotionFsm2")
local pending, next_check = true, 0
local status, report = "Waiting for the local player...", nil

local function scan_runtime()
    local manager = sdk.get_managed_singleton("snow.player.PlayerManager")
    local player = manager and manager:call("findMasterPlayer")
    if not player then return nil end
    local game_object = player:call("get_GameObject")
    local fsm = game_object and game_object:call("getComponent(System.Type)", fsm_type)
    local layer = fsm and fsm:call("getLayer", 0)
    local tree = layer and layer:get_tree_object()
    if not tree then return nil end

    local result = {
        checked_at = os.date("%Y-%m-%d %H:%M:%S"),
        layer = 0,
        action_count = tree:get_action_count(),
        condition_count = tree:get_condition_count(),
        node_count = tree:get_node_count(),
        checks = {},
        passed = true,
    }
    local function check(name, value)
        local passed = value == true
        table.insert(result.checks, { name = name, passed = passed })
        result.passed = result.passed and passed
    end
    check("Counts: 9786 Actions / 7166 Conditions / 4479 Nodes",
        result.action_count == 9786 and result.condition_count == 7166 and result.node_count == 4479)

    local action = result.action_count > ACTION_INDEX and tree:get_action(ACTION_INDEX)
    local condition = result.condition_count > CONDITION_INDEX and tree:get_condition(CONDITION_INDEX)
    local node = result.node_count > NODE_INDEX and tree:get_node(NODE_INDEX)
    check("New Action instance retained", action ~= nil and action ~= false
        and action_id:call(action) == ACTION_ID
        and action:get_type_definition():get_full_name() == "snow.player.fsm.PlayerFsm2ActionDogRideAccess")
    local condition_matches = condition ~= nil and condition ~= false
        and condition_id:call(condition) == CONDITION_ID
        and condition:get_type_definition():get_full_name() == "snow.player.fsm.PlayerFsm2ConditionCheckMotionFrame"
    check("New Condition instance loaded", condition_matches)
    if condition_matches then
        result.condition = {
            id = string.format("0x%08X", condition_id:call(condition)),
            address = string.format("0x%X", condition:get_address()),
            condition_value = condition:call("getCondition"),
            start_frame = condition:get_field("_StartFrame"),
            end_frame = condition:get_field("_EndFrame"),
        }
        check("Condition fields: false / 12.5 / 24.5", result.condition.condition_value == false
            and result.condition.start_frame == 12.5 and result.condition.end_frame == 24.5)
    else
        check("Condition fields: false / 12.5 / 24.5", false)
    end

    local node_matches = node ~= nil and node ~= false
        and node:get_id() == NODE_ID and node:get_name() == "reasy_test_node"
    check("New Node ID and name loaded", node_matches)
    local root = result.node_count > 0 and tree:get_node(0)
    local root_links_node = false
    if root then
        for _, child in ipairs(root:get_children()) do
            if child:get_id() == NODE_ID then root_links_node = true end
        end
    end
    check("Root children include new Node", root_links_node)
    if node_matches then
        result.node = { id = string.format("0x%X", node:get_id()), name = node:get_full_name() }
        local parent = node:get_parent()
        check("New Node parent is root", parent ~= nil and parent:get_id() == 0)
        local actions = node:get_actions()
        result.node.action_count = #actions
        check("New Node references the new Action object", #actions == 1 and action ~= nil and action ~= false
            and actions[1]:get_address() == action:get_address() and action_id:call(actions[1]) == ACTION_ID)
        local data = node:get_data()
        local conditions, states = data:get_transition_conditions(), data:get_states()
        local linked_condition = conditions:size() == 1 and tree:get_condition(conditions[0])
        check("New Node transition references new Condition object", condition_matches
            and linked_condition ~= nil and linked_condition ~= false
            and linked_condition:get_address() == condition:get_address())
        local target = states:size() == 1 and tree:get_node(states[0])
        check("New Node transition target is wait", target ~= nil and target ~= false
            and target:get_id() == 0x3A985507 and target:get_name() == "wait")
    else
        check("New Node parent is root", false)
        check("New Node references the new Action object", false)
        check("New Node transition references new Condition object", false)
        check("New Node transition target is wait", false)
    end
    local wait = result.node_count > 1 and tree:get_node(1)
    local actions = wait and wait:get_actions() or {}
    result.wait_action_count = #actions
    check("wait reference removed; original two Actions retained", #actions == 2
        and action_id:call(actions[1]) == 0xE782A7D1 and action_id:call(actions[2]) == 0xED751E56)
    return result
end

re.on_pre_application_entry("UpdateScene", function()
    if not pending or os.clock() < next_check then return end
    next_check = os.clock() + 2
    local ok, result = pcall(scan_runtime)
    if ok and result == nil then return end
    pending = false
    if not ok then
        report = { error = tostring(result) }
        status = "ERROR: " .. tostring(result)
    else
        report = result
        status = result.passed and "PASS: all runtime structure checks passed."
            or "NOT PASSED: reload the FSM, then Check again."
    end
    log.info("[REasy FSM Structure Check] " .. status)
    json.dump_file(REPORT_PATH, report)
end)

re.on_draw_ui(function()
    if not imgui.tree_node("REasy FSM Structure Check") then return end
    imgui.text(status)
    if imgui.button("Check again##reasy_structure") then
        pending, next_check, report = true, 0, nil
        status = "Checking on the next UpdateScene..."
    end
    if report and report.checks then
        imgui.text("Checked: " .. report.checked_at)
        imgui.text(string.format("Actions %d / Conditions %d / Nodes %d",
            report.action_count, report.condition_count, report.node_count))
        for _, check in ipairs(report.checks) do
            imgui.text((check.passed and "[PASS] " or "[FAIL] ") .. check.name)
        end
    end
    imgui.text("Report: reframework/data/" .. REPORT_PATH)
    imgui.tree_pop()
end)
