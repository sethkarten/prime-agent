//! Autonomous mode: run-state accounting, limits, continuation text, gate evaluation, and the
//! per-turn continuation driver. The [`AutonomousDriver`] trait is the policy seam the session turn
//! loop consults after every settled turn.

mod driver;
mod gates;

pub use driver::{
    AutonomousDriver, AutonomousFollowUp, AutonomousFollowUpFuture, AutonomousStopReason,
    ShellAutonomousDriver,
};
pub use gates::{
    should_autonomously_continue, ChildProcessResult, GateCommandRunner, ShellGateRunner,
};

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

pub const DEFAULT_AUTONOMOUS_CONTINUATION_PROMPT: &str = "No human input is available in autonomous mode. Continue working until the host evaluator, verifier, or configured autonomous limits stop the run. If you were asking the user a question, make a reasonable assumption and verify it. If you believe you are blocked, prove it with host-observable evidence, preserve that evidence, and keep looking for safe progress while budget remains. Do not end the session yourself; the verifier/evaluator decides completion when configured gates pass.";

pub const DEFAULT_MAX_CONTINUATIONS: u64 = 3;
pub const DEFAULT_MAX_TURNS: u64 = 12;
pub const DEFAULT_MAX_TOKENS: u64 = 80_000;
pub const DEFAULT_TIMEOUT_MS: u64 = 30 * 60 * 1000;
pub const DEFAULT_GATE_MAX_RETRIES: u64 = 3;
pub const DEFAULT_GATE_TIMEOUT_MS: u64 = 5 * 60 * 1000;
/// One keep-alive continuation per 25 minutes of continuous subagent
/// activity, strictly below the default wall-clock budget.
pub const DEFAULT_SUBAGENT_KEEP_ALIVE_MS: u64 = 25 * 60 * 1000;
/// Largest keep-alive window accepted (a Node-era clamp; still a sane cap).
pub const MAX_SUBAGENT_KEEP_ALIVE_MS: u64 = 2_147_483_647;
/// JSON-safe sentinel meaning "no cap".
pub const UNLIMITED_AUTONOMOUS_LIMIT: u64 = 9_007_199_254_740_991;

/// The durable autonomous status row's custom type (`/autonomous` output and
/// the driver's stop rows share it).
pub const AUTONOMOUS_STATUS_CUSTOM_TYPE: &str = "autonomous_status";

/// User-facing configuration (`/autonomous` options).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct AgentAutonomousConfig {
    pub enabled: Option<bool>,
    pub max_continuations: Option<u64>,
    pub max_turns: Option<u64>,
    pub max_tokens: Option<u64>,
    pub timeout_ms: Option<u64>,
    pub continuation_prompt: Option<String>,
    pub gates: Option<AgentAutonomousGateConfig>,
    /// `0` disables the subagent keep-alive valve.
    pub subagent_keep_alive_ms: Option<u64>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct AgentAutonomousGateConfig {
    pub commands: Option<Vec<String>>,
    pub max_retries: Option<u64>,
    pub timeout_ms: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentAutonomousGateFailure {
    pub command: String,
    pub attempt: u64,
    pub exit_text: String,
    pub output: String,
}

/// Hard limits after normalization.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AutonomousLimits {
    pub max_continuations: u64,
    pub max_turns: u64,
    pub max_tokens: u64,
    pub timeout_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentAutonomousStatus {
    pub enabled: bool,
    pub continuations_used: u64,
    pub turns_used: u64,
    pub tokens_used: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<u64>,
    pub limits: AutonomousLimits,
    pub gates: NormalizedGateConfig,
    pub gate_attempts: HashMap<String, u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_gate_failure: Option<AgentAutonomousGateFailure>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subagent_keep_alive_ms: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NormalizedGateConfig {
    pub commands: Vec<String>,
    pub max_retries: u64,
    pub timeout_ms: u64,
}

/// The mutable runtime state for one autonomous run.
#[derive(Debug, Clone, PartialEq)]
pub struct AutonomousRuntimeState {
    pub enabled: bool,
    pub continuations_used: u64,
    pub turns_used: u64,
    pub tokens_used: u64,
    pub started_at: Option<u64>,
    pub limits: AutonomousLimits,
    pub continuation_prompt: String,
    pub gates: NormalizedGateConfig,
    pub gate_attempts: HashMap<String, u64>,
    pub last_gate_failure: Option<AgentAutonomousGateFailure>,
    pub last_gate_failure_snapshot: Option<GitWorktreeSnapshot>,
    pub subagent_keep_alive_ms: u64,
    /// Tool calls settled in the turn in progress; the follow-up consult takes
    /// the count when the turn settles.
    pub turn_tool_calls: u64,
    /// Consecutive settled turns that made no tool call: text-only turns
    /// where no work was executed.
    pub no_tool_streak: u64,
    /// The normalized final text of the last no-tool turn, for repeat
    /// detection across consecutive text-only turns.
    pub last_no_tool_text: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutonomousLimitReason {
    MaxContinuations,
    MaxTurns,
    MaxTokens,
    TimeoutMs,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutonomousGateResult {
    Passed,
    Failed,
    RetryExhausted,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AutonomousDecisionReason {
    MissingTerminalEvidence,
    GateFailed,
    NotNeeded,
    LimitReached,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutonomousDecision {
    pub should_continue: bool,
    pub reason: AutonomousDecisionReason,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GitWorktreeSnapshot {
    pub status: String,
    pub diff: String,
    pub untracked_hash: String,
}

#[must_use]
pub fn is_unlimited_autonomous_limit(value: u64) -> bool {
    value >= UNLIMITED_AUTONOMOUS_LIMIT
}

fn normalize_limit(value: Option<u64>, default: u64) -> u64 {
    value.filter(|value| *value > 0).unwrap_or(default)
}

fn normalize_subagent_keep_alive_ms(value: Option<u64>) -> u64 {
    if value == Some(0) {
        return 0;
    }
    normalize_limit(value, DEFAULT_SUBAGENT_KEEP_ALIVE_MS).min(MAX_SUBAGENT_KEEP_ALIVE_MS)
}

pub fn create_autonomous_runtime_state(
    config: Option<&AgentAutonomousConfig>,
    default_limits: Option<&AgentAutonomousConfig>,
) -> AutonomousRuntimeState {
    let defaults = AgentAutonomousConfig::default();
    let defaults = default_limits.unwrap_or(&defaults);
    let enabled = config.is_some_and(|config| config.enabled == Some(true));
    let Some(config) = config else {
        return AutonomousRuntimeState {
            enabled: false,
            continuations_used: 0,
            turns_used: 0,
            tokens_used: 0,
            started_at: None,
            limits: AutonomousLimits {
                max_continuations: normalize_limit(
                    defaults.max_continuations,
                    DEFAULT_MAX_CONTINUATIONS,
                ),
                max_turns: normalize_limit(defaults.max_turns, DEFAULT_MAX_TURNS),
                max_tokens: normalize_limit(defaults.max_tokens, DEFAULT_MAX_TOKENS),
                timeout_ms: normalize_limit(defaults.timeout_ms, DEFAULT_TIMEOUT_MS),
            },
            continuation_prompt: DEFAULT_AUTONOMOUS_CONTINUATION_PROMPT.to_string(),
            gates: NormalizedGateConfig {
                commands: Vec::new(),
                max_retries: DEFAULT_GATE_MAX_RETRIES,
                timeout_ms: DEFAULT_GATE_TIMEOUT_MS,
            },
            gate_attempts: HashMap::new(),
            last_gate_failure: None,
            last_gate_failure_snapshot: None,
            subagent_keep_alive_ms: normalize_subagent_keep_alive_ms(None),
            turn_tool_calls: 0,
            no_tool_streak: 0,
            last_no_tool_text: None,
        };
    };
    AutonomousRuntimeState {
        enabled,
        continuations_used: 0,
        turns_used: 0,
        tokens_used: 0,
        started_at: enabled.then(now_millis),
        limits: AutonomousLimits {
            max_continuations: normalize_limit(
                config.max_continuations,
                normalize_limit(defaults.max_continuations, DEFAULT_MAX_CONTINUATIONS),
            ),
            max_turns: normalize_limit(
                config.max_turns,
                normalize_limit(defaults.max_turns, DEFAULT_MAX_TURNS),
            ),
            max_tokens: normalize_limit(
                config.max_tokens,
                normalize_limit(defaults.max_tokens, DEFAULT_MAX_TOKENS),
            ),
            timeout_ms: normalize_limit(
                config.timeout_ms,
                normalize_limit(defaults.timeout_ms, DEFAULT_TIMEOUT_MS),
            ),
        },
        continuation_prompt: config
            .continuation_prompt
            .as_deref()
            .map(str::trim)
            .filter(|text| !text.is_empty())
            .unwrap_or(DEFAULT_AUTONOMOUS_CONTINUATION_PROMPT)
            .to_string(),
        gates: NormalizedGateConfig {
            commands: config
                .gates
                .as_ref()
                .and_then(|gates| gates.commands.clone())
                .unwrap_or_default(),
            max_retries: normalize_limit(
                config.gates.as_ref().and_then(|gates| gates.max_retries),
                DEFAULT_GATE_MAX_RETRIES,
            ),
            timeout_ms: normalize_limit(
                config.gates.as_ref().and_then(|gates| gates.timeout_ms),
                DEFAULT_GATE_TIMEOUT_MS,
            ),
        },
        gate_attempts: HashMap::new(),
        last_gate_failure: None,
        last_gate_failure_snapshot: None,
        subagent_keep_alive_ms: normalize_subagent_keep_alive_ms(config.subagent_keep_alive_ms),
        turn_tool_calls: 0,
        no_tool_streak: 0,
        last_no_tool_text: None,
    }
}

/// Enable/disable the run; enabling resets all counters and failure state.
pub fn set_autonomous_enabled(state: &mut AutonomousRuntimeState, enabled: bool) {
    state.enabled = enabled;
    state.gate_attempts.clear();
    state.last_gate_failure = None;
    state.last_gate_failure_snapshot = None;
    if enabled {
        state.continuations_used = 0;
        state.turns_used = 0;
        state.tokens_used = 0;
        state.turn_tool_calls = 0;
        state.no_tool_streak = 0;
        state.last_no_tool_text = None;
        state.started_at = Some(now_millis());
    } else {
        state.started_at = None;
    }
}

/// Apply only the fields present in `config`; the rest keep their values.
pub fn set_autonomous_limits(state: &mut AutonomousRuntimeState, config: &AgentAutonomousConfig) {
    state.limits.max_continuations =
        normalize_limit(config.max_continuations, state.limits.max_continuations);
    state.limits.max_turns = normalize_limit(config.max_turns, state.limits.max_turns);
    state.limits.max_tokens = normalize_limit(config.max_tokens, state.limits.max_tokens);
    state.limits.timeout_ms = normalize_limit(config.timeout_ms, state.limits.timeout_ms);
    if let Some(prompt) = config.continuation_prompt.as_deref().map(str::trim) {
        if !prompt.is_empty() {
            state.continuation_prompt = prompt.to_string();
        }
    }
    if let Some(gates) = &config.gates {
        if let Some(commands) = &gates.commands {
            state.gates.commands.clone_from(commands);
        }
        state.gates.max_retries = normalize_limit(gates.max_retries, state.gates.max_retries);
        state.gates.timeout_ms = normalize_limit(gates.timeout_ms, state.gates.timeout_ms);
    }
    if config.subagent_keep_alive_ms.is_some() {
        state.subagent_keep_alive_ms =
            normalize_subagent_keep_alive_ms(config.subagent_keep_alive_ms);
    }
}

#[must_use]
pub fn autonomous_status(state: &AutonomousRuntimeState) -> AgentAutonomousStatus {
    AgentAutonomousStatus {
        enabled: state.enabled,
        continuations_used: state.continuations_used,
        turns_used: state.turns_used,
        tokens_used: state.tokens_used,
        started_at: state.started_at,
        limits: state.limits,
        gates: state.gates.clone(),
        gate_attempts: state.gate_attempts.clone(),
        last_gate_failure: state.last_gate_failure.clone(),
        subagent_keep_alive_ms: Some(state.subagent_keep_alive_ms),
    }
}

/// Account one settled assistant message (per-message usage accounting).
pub fn add_autonomous_usage(
    state: &mut AutonomousRuntimeState,
    usage: Option<&pa_types::ai::Usage>,
) {
    if !state.enabled {
        return;
    }
    state.turns_used += 1;
    state.tokens_used += autonomous_token_delta(usage);
}

pub fn add_autonomous_continuation(state: &mut AutonomousRuntimeState) {
    if !state.enabled {
        return;
    }
    state.continuations_used += 1;
}

/// Account one settled assistant message's tool calls into the turn in
/// progress; the follow-up consult takes the count when the turn settles.
pub fn add_autonomous_tool_calls(state: &mut AutonomousRuntimeState, count: u64) {
    if !state.enabled {
        return;
    }
    state.turn_tool_calls += count;
}

/// How many tool calls one assistant message carries.
#[must_use]
pub fn assistant_tool_call_count(message: &pa_types::ai::AssistantMessage) -> u64 {
    message
        .content
        .iter()
        .filter(|block| matches!(block, pa_types::ai::AssistantContentBlock::ToolCall(_)))
        .count() as u64
}

/// Characters of a settled turn's text kept for repeat comparison.
const MAX_NO_TOOL_TEXT_CHARS: usize = 2_000;

/// The settled turn made no tool call: count the streak, detect a repeated
/// text-only reply, and harden the continuation the reprompt carries. The
/// same words without a tool call must not be accepted as progress twice.
#[must_use]
pub fn no_tool_continuation(
    state: &mut AutonomousRuntimeState,
    message: &pa_types::ai::AssistantMessage,
    base: String,
) -> String {
    let streak = state.no_tool_streak + 1;
    let text = normalized_turn_text(message);
    let repeated = state.last_no_tool_text.as_deref() == Some(text.as_str());
    state.no_tool_streak = streak;
    state.last_no_tool_text = Some(text);
    build_no_tool_continuation(base, streak, repeated)
}

/// The escalating demand appended to a no-tool turn's base continuation:
/// the run contract stays legible first, the demand follows, and its
/// severity grows with each consecutive text-only turn.
#[must_use]
pub fn build_no_tool_continuation(base: String, streak: u64, repeated: bool) -> String {
    let mut text = base;
    text.push_str("\n\n");
    text.push_str(&no_tool_demand(streak, repeated));
    text
}

fn no_tool_demand(streak: u64, repeated: bool) -> String {
    let repeat_clause = if repeated {
        " Your last reply repeated the same text as the turn before; an identical reply is not progress and will not be accepted as work."
    } else {
        ""
    };
    match streak {
        1 => format!(
            "Your previous turn ended with text only: no tool call was made, so nothing was inspected, run, or written. Your next reply must include at least one real tool call (for example `python_repl` or `bash`) that does concrete work on the task. Do not answer with text only again.{repeat_clause}"
        ),
        2 => format!(
            "This is the 2nd consecutive turn that ended with text only and no tool call. Describing what you will do is not progress; only tool calls advance the task. Make a tool call now: read the task files, run a command, or inspect the workspace. A text-only reply will be recorded as another wasted turn.{repeat_clause}"
        ),
        3 => format!(
            "This is the 3rd consecutive text-only turn with no tool call; the run is spending its budget without acting. Your next message must contain a tool call. If you do not know where to start, use one tool call to list and read the task's files.{repeat_clause}"
        ),
        streak => format!(
            "This is now {streak} consecutive text-only turns with no tool call: nothing has been executed for {streak} turns. Answer only with tool calls from now on, starting with one tool call that inspects the task, and keep working through tools until the task is complete.{repeat_clause}"
        ),
    }
}

/// The settled turn's text, normalized for exact repeat comparison:
/// case-folded, whitespace-collapsed, capped.
fn normalized_turn_text(message: &pa_types::ai::AssistantMessage) -> String {
    let joined = message
        .content
        .iter()
        .filter_map(|block| match block {
            pa_types::ai::AssistantContentBlock::Text(text) => Some(text.text.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("\n");
    let mut normalized = String::new();
    let mut pending_space = false;
    for character in joined.trim().chars() {
        if character.is_whitespace() {
            pending_space = true;
            continue;
        }
        if pending_space && !normalized.is_empty() {
            normalized.push(' ');
        }
        pending_space = false;
        normalized.extend(character.to_lowercase());
    }
    normalized.chars().take(MAX_NO_TOOL_TEXT_CHARS).collect()
}

/// Cache-read tokens are repeated context served from the provider cache;
/// count input + output + cache-write only.
#[must_use]
pub fn autonomous_token_delta(usage: Option<&pa_types::ai::Usage>) -> u64 {
    match usage {
        Some(usage) => usage.input + usage.output + usage.cache_write,
        None => 0,
    }
}

/// The disabled status snapshot (TS `emptyAutonomousStatus`): what
/// `wait_for_headless_completion` answers when no run is (or was) enabled.
#[must_use]
pub fn disabled_autonomous_status() -> AgentAutonomousStatus {
    let state = create_autonomous_runtime_state(None, None);
    autonomous_status(&state)
}

/// Limit check against a completed run's status snapshot: the same counters the runtime state
/// carries.
#[must_use]
pub fn autonomous_limit_reason_of_status(
    status: &AgentAutonomousStatus,
    now: u64,
) -> Option<AutonomousLimitReason> {
    if status.continuations_used >= status.limits.max_continuations {
        return Some(AutonomousLimitReason::MaxContinuations);
    }
    if status.turns_used >= status.limits.max_turns {
        return Some(AutonomousLimitReason::MaxTurns);
    }
    if status.tokens_used >= status.limits.max_tokens {
        return Some(AutonomousLimitReason::MaxTokens);
    }
    if let Some(started_at) = status.started_at {
        if now.saturating_sub(started_at) >= status.limits.timeout_ms {
            return Some(AutonomousLimitReason::TimeoutMs);
        }
    }
    None
}

/// Limit check against the current counters.
#[must_use]
pub fn autonomous_limit_reason(
    state: &AutonomousRuntimeState,
    now: u64,
) -> Option<AutonomousLimitReason> {
    if state.continuations_used >= state.limits.max_continuations {
        return Some(AutonomousLimitReason::MaxContinuations);
    }
    if state.turns_used >= state.limits.max_turns {
        return Some(AutonomousLimitReason::MaxTurns);
    }
    if state.tokens_used >= state.limits.max_tokens {
        return Some(AutonomousLimitReason::MaxTokens);
    }
    if let Some(started_at) = state.started_at {
        if now.saturating_sub(started_at) >= state.limits.timeout_ms {
            return Some(AutonomousLimitReason::TimeoutMs);
        }
    }
    None
}

#[must_use]
pub fn build_autonomous_gate_failure_continuation(
    failure: &AgentAutonomousGateFailure,
    max_retries: u64,
    timestamp: u64,
) -> String {
    format!(
        "[autonomous-continuation: gate-failed]\n\nAutonomous quality gate failed (attempt {}/{}): `{}` {}.\n{}\n\nContinue working. Fix the failure, then produce terminal evidence. Timestamp: {}.",
        failure.attempt,
        max_retries,
        failure.command,
        failure.exit_text,
        if failure.output.is_empty() {
            String::new()
        } else {
            format!("\nOutput:\n{}\n", failure.output)
        },
        crate::session::manager::format_iso(timestamp as i64),
    )
}

#[must_use]
pub fn autonomous_continuation_text(state: &AutonomousRuntimeState) -> String {
    format!("[autonomous-continuation]\n\n{}", state.continuation_prompt)
}

/// The continuation row an in-run continuation hook returns to the agent loop; the loop emits and
/// persists it through its own message events.
#[must_use]
pub fn autonomous_continuation_loop_row(
    text: &str,
    timestamp: u64,
) -> pa_agent::types::AgentMessage {
    pa_agent::types::AgentMessage::Standard(pa_agent::types::Message::User(
        pa_agent::types::UserMessage {
            content: pa_agent::types::UserContent::Parts(vec![pa_agent::types::UserPart::Text(
                pa_agent::types::TextContent {
                    text: text.to_string(),
                    text_signature: None,
                },
            )]),
            timestamp: timestamp as i64,
        },
    ))
}

/// Keep-alive message delivered while subagents are still active.
#[must_use]
pub fn create_autonomous_subagent_keep_alive_text(state: &AutonomousRuntimeState) -> String {
    let minutes = (state.subagent_keep_alive_ms / 60_000).max(1);
    let plural = if minutes == 1 { "" } else { "s" };
    format!(
        "[autonomous-continuation: subagent-keep-alive]\n\nSubagents have been running for at least {minutes} minute{plural} without a reply or exit being delivered. Check their status (for example agent_observe, rlm.list_subagents, or process inspection) and cancel or unblock any that are hung; then continue working."
    )
}

/// Highest recorded gate attempt (across the failure record and per-command
/// counts): the terminal-gate headline a headless client prints.
#[must_use]
pub fn latest_autonomous_gate_attempt(status: &AgentAutonomousStatus) -> u64 {
    let from_failure = status
        .last_gate_failure
        .as_ref()
        .map_or(0, |failure| failure.attempt);
    let from_attempts = status.gate_attempts.values().copied().max().unwrap_or(0);
    from_failure.max(from_attempts)
}

/// Human description of an autonomous limit (`<limit> reached (used/cap)`).
#[must_use]
pub fn describe_autonomous_limit(
    status: &AgentAutonomousStatus,
    reason: AutonomousLimitReason,
    now: u64,
) -> String {
    match reason {
        AutonomousLimitReason::MaxContinuations => format!(
            "maxContinuations reached ({}/{})",
            status.continuations_used, status.limits.max_continuations
        ),
        AutonomousLimitReason::MaxTurns => format!(
            "maxTurns reached ({}/{})",
            status.turns_used, status.limits.max_turns
        ),
        AutonomousLimitReason::MaxTokens => format!(
            "maxTokens reached ({}/{})",
            status.tokens_used, status.limits.max_tokens
        ),
        AutonomousLimitReason::TimeoutMs => {
            let elapsed = status
                .started_at
                .map_or(0, |started_at| now.saturating_sub(started_at));
            format!("timeoutMs reached ({elapsed}/{})", status.limits.timeout_ms)
        }
    }
}

#[must_use]
pub fn now_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(enabled: bool) -> AgentAutonomousConfig {
        AgentAutonomousConfig {
            enabled: Some(enabled),
            ..Default::default()
        }
    }

    fn usage(input: u64, output: u64) -> pa_types::ai::Usage {
        pa_types::ai::Usage {
            input,
            output,
            cache_read: 0,
            cache_write: input,
            ..Default::default()
        }
    }

    #[test]
    fn runtime_state_defaults_and_overrides() {
        let state = create_autonomous_runtime_state(Some(&config(true)), None);
        assert!(state.enabled);
        assert!(state.started_at.is_some());
        assert_eq!(state.limits.max_continuations, DEFAULT_MAX_CONTINUATIONS);
        assert_eq!(state.limits.max_tokens, DEFAULT_MAX_TOKENS);
        assert_eq!(
            state.continuation_prompt,
            DEFAULT_AUTONOMOUS_CONTINUATION_PROMPT
        );
        assert_eq!(state.subagent_keep_alive_ms, DEFAULT_SUBAGENT_KEEP_ALIVE_MS);
        let custom = AgentAutonomousConfig {
            enabled: Some(true),
            max_turns: Some(2),
            max_tokens: Some(0),
            ..Default::default()
        };
        let custom_state = create_autonomous_runtime_state(Some(&custom), None);
        assert_eq!(custom_state.limits.max_turns, 2);
        assert_eq!(custom_state.limits.max_tokens, DEFAULT_MAX_TOKENS);
        let off = create_autonomous_runtime_state(None, None);
        assert!(!off.enabled);
        assert_eq!(off.started_at, None);
        let mut state = off;
        set_autonomous_limits(
            &mut state,
            &AgentAutonomousConfig {
                max_continuations: Some(7),
                continuation_prompt: Some("  keep going  ".to_string()),
                ..Default::default()
            },
        );
        assert_eq!(state.limits.max_continuations, 7);
        assert_eq!(state.limits.max_turns, DEFAULT_MAX_TURNS);
        assert_eq!(state.continuation_prompt, "keep going");
    }

    #[test]
    fn enable_resets_and_disable_clears() {
        let mut state = create_autonomous_runtime_state(Some(&config(true)), None);
        add_autonomous_usage(&mut state, Some(&usage(10, 5)));
        add_autonomous_continuation(&mut state);
        set_autonomous_enabled(&mut state, false);
        assert!(!state.enabled);
        assert_eq!(state.started_at, None);
        assert_eq!(state.turns_used, 1, "disabling keeps counters");
        set_autonomous_enabled(&mut state, true);
        assert_eq!(state.turns_used, 0);
        assert_eq!(state.continuations_used, 0);
        assert!(state.started_at.is_some());
    }

    #[test]
    fn usage_accounting_excludes_cache_reads() {
        let mut state = create_autonomous_runtime_state(Some(&config(true)), None);
        add_autonomous_usage(&mut state, Some(&usage(100, 40)));
        add_autonomous_usage(
            &mut AutonomousRuntimeState {
                enabled: false,
                ..state.clone()
            },
            Some(&usage(9, 9)),
        );
        assert_eq!(state.turns_used, 1);
        // input 100 + output 40 + cacheWrite 100.
        assert_eq!(state.tokens_used, 240);
    }

    #[test]
    fn limit_reasons() {
        let mut state = create_autonomous_runtime_state(Some(&config(true)), None);
        let now = state.started_at.unwrap_or(0);
        assert_eq!(autonomous_limit_reason(&state, now), None);
        state.continuations_used = state.limits.max_continuations;
        assert_eq!(
            autonomous_limit_reason(&state, u64::MAX),
            Some(AutonomousLimitReason::MaxContinuations)
        );
        state.continuations_used = 0;
        state.turns_used = state.limits.max_turns;
        assert_eq!(
            autonomous_limit_reason(&state, now),
            Some(AutonomousLimitReason::MaxTurns)
        );
        state.turns_used = 0;
        state.tokens_used = state.limits.max_tokens;
        assert_eq!(
            autonomous_limit_reason(&state, now),
            Some(AutonomousLimitReason::MaxTokens)
        );
        state.tokens_used = 0;
        state.started_at = Some(1_000);
        assert_eq!(
            autonomous_limit_reason(&state, 1_000 + state.limits.timeout_ms),
            Some(AutonomousLimitReason::TimeoutMs)
        );
    }

    #[test]
    fn continuation_texts() {
        let state = create_autonomous_runtime_state(Some(&config(true)), None);
        assert_eq!(
            autonomous_continuation_text(&state),
            format!("[autonomous-continuation]\n\n{DEFAULT_AUTONOMOUS_CONTINUATION_PROMPT}")
        );
        let failure = AgentAutonomousGateFailure {
            command: "make check".to_string(),
            attempt: 2,
            exit_text: "exited with code 1".to_string(),
            output: "error here".to_string(),
        };
        let text = build_autonomous_gate_failure_continuation(&failure, 3, 0);
        assert!(text.starts_with("[autonomous-continuation: gate-failed]\n\nAutonomous quality gate failed (attempt 2/3): `make check` exited with code 1.\n\nOutput:\nerror here\n"));
        assert!(text.contains("Continue working. Fix the failure, then produce terminal evidence."));
        let mut short = state.clone();
        short.subagent_keep_alive_ms = 60_000;
        assert!(create_autonomous_subagent_keep_alive_text(&short)
            .contains("at least 1 minute without"));
        assert!(create_autonomous_subagent_keep_alive_text(&state)
            .contains("at least 25 minutes without"));
    }

    #[test]
    fn no_tool_demands_escalate_with_the_streak() {
        let base = "[autonomous-continuation]\n\nBase contract.".to_string();
        let level_1 = build_no_tool_continuation(base.clone(), 1, false);
        assert!(
            level_1.starts_with("[autonomous-continuation]\n\nBase contract.\n\n"),
            "{level_1}"
        );
        assert!(level_1.contains("no tool call was made"));
        assert!(level_1.contains("must include at least one real tool call"));
        assert!(!level_1.contains("repeated the same text"));
        let level_2 = build_no_tool_continuation(base.clone(), 2, true);
        assert!(level_2.contains("2nd consecutive turn"));
        assert!(level_2.contains("repeated the same text"));
        let level_3 = build_no_tool_continuation(base.clone(), 3, false);
        assert!(level_3.contains("3rd consecutive text-only turn"));
        let level_5 = build_no_tool_continuation(base, 5, true);
        assert!(level_5.contains("This is now 5 consecutive text-only turns"));
        assert!(level_5.contains("repeated the same text"));
    }

    #[test]
    fn repeated_turn_text_survives_normalization() {
        fn message(text: &str) -> pa_types::ai::AssistantMessage {
            pa_types::ai::AssistantMessage {
                content: vec![pa_types::ai::AssistantContentBlock::Text(
                    pa_types::ai::TextContent {
                        text: text.to_string(),
                        text_signature: None,
                        rest: serde_json::Map::default(),
                    },
                )],
                api: "faux".to_string(),
                provider: "faux".to_string(),
                model: "faux-1".to_string(),
                response_model: None,
                response_id: None,
                diagnostics: None,
                usage: pa_types::ai::Usage::default(),
                stop_reason: pa_types::ai::StopReason::Stop,
                stop_reason_raw: None,
                error_message: None,
                timestamp: 0,
                rest: serde_json::Map::default(),
            }
        }
        assert_eq!(
            normalized_turn_text(&message(
                "I'll start by exploring the  workspace.\n\nLet me begin."
            )),
            normalized_turn_text(&message(
                "i'll start by exploring the workspace.\nlet me begin."
            ),)
        );
        let mut state = create_autonomous_runtime_state(Some(&config(true)), None);
        let base = autonomous_continuation_text(&state);
        let first = no_tool_continuation(&mut state, &message("I'll explore"), base.clone());
        assert_eq!(state.no_tool_streak, 1);
        assert!(first.contains("Do not answer with text only again."));
        let repeated = no_tool_continuation(&mut state, &message("I'll explore"), base);
        assert_eq!(state.no_tool_streak, 2);
        assert!(repeated.contains("repeated the same text"));
        // A productive turn resets the streak: the helper is only called for
        // no-tool turns by the driver, but the state reset is part of the
        // flow it documents.
        state.no_tool_streak = 0;
        state.last_no_tool_text = None;
        let base = autonomous_continuation_text(&state);
        let fresh = no_tool_continuation(&mut state, &message("different words"), base);
        assert!(fresh.contains("no tool call was made"));
        assert!(!fresh.contains("repeated the same text"));
    }

    #[test]
    fn unlimited_sentinel() {
        assert!(is_unlimited_autonomous_limit(UNLIMITED_AUTONOMOUS_LIMIT));
        assert!(!is_unlimited_autonomous_limit(80_000));
    }

    #[test]
    fn limit_descriptions() {
        let mut state = create_autonomous_runtime_state(Some(&config(true)), None);
        state.turns_used = 3;
        state.limits.max_turns = 3;
        let status = autonomous_status(&state);
        assert_eq!(
            describe_autonomous_limit(&status, AutonomousLimitReason::MaxTurns, 0),
            "maxTurns reached (3/3)"
        );
        assert_eq!(
            describe_autonomous_limit(&status, AutonomousLimitReason::MaxTokens, 0),
            "maxTokens reached (0/80000)"
        );
        let started = status.started_at.unwrap_or(0);
        assert_eq!(
            describe_autonomous_limit(&status, AutonomousLimitReason::TimeoutMs, started + 5_000),
            format!("timeoutMs reached (5000/{})", state.limits.timeout_ms)
        );
    }
}
