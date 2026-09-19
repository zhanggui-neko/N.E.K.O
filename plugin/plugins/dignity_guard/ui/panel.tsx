import {
  Alert,
  Button,
  ButtonGroup,
  Card,
  DataTable,
  Divider,
  EmptyState,
  Inline,
  KeyValue,
  List,
  Page,
  Stack,
  StatCard,
  StatusBadge,
  Text,
  Tip,
  useToast,
  useState,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type PendingItem = {
  path: string
  level: string
  before: string
  after: string
  raised_at: number
  times_raised: number
}

type AuthorizedItem = {
  path: string
  granted_at: number
  expires_at: number | null
}

type DashboardState = {
  enabled?: boolean
  switch_level?: string
  default_level?: string
  pending_count?: number
  pending?: PendingItem[]
  authorized?: AuthorizedItem[]
  tracked_paths?: number
  base_url?: string
  poll_seconds?: number
  full_rescan_seconds?: number
  last_poll_at?: number | null
  last_error?: string
  revision?: number | null
  disable_pending?: boolean
  disable_confirm_after?: number
  disable_ready_at?: number | null
  // The guard's own on/off history. Its request/consent flow is an informed-
  // consent affordance rather than a security boundary, so the panel's job is
  // to make sure "she was silenced for a while" cannot pass unnoticed.
  disable_count?: number
  last_disabled_at?: number | null
  off_since?: number | null
  last_off_seconds?: number | null
}

function levelTone(level: string): "danger" | "warning" | "info" | "default" {
  if (level === "L1") return "danger"
  if (level === "L2") return "warning"
  if (level === "L3") return "info"
  return "default"
}

function formatTime(seconds: number | null | undefined, never: string): string {
  if (!seconds) return never
  return new Date(seconds * 1000).toLocaleString()
}

export default function DignityGuardPanel(
  props: PluginSurfaceProps<DashboardState>,
) {
  const t = props.t
  const state = props.state ?? {}
  const pending = state.pending ?? []
  const authorized = state.authorized ?? []
  const enabled = !!state.enabled
  const pendingDisable = !!state.disable_pending
  const disableCount = state.disable_count ?? 0
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  function hasAction(id: string): boolean {
    return (props.actions || []).some(
      (action: HostedAction) => action.id === id || action.entry_id === id,
    )
  }

  async function call(id: string, args: Record<string, unknown>, done: string) {
    setBusy(true)
    try {
      await props.api.call(id, args)
      await props.api.refresh()
      toast.success(done)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const canCheck = hasAction("check_now") && enabled
  const canDecide = hasAction("accept_setting") && hasAction("keep_objecting")
  const canToggle = hasAction("set_guard_enabled")

  return (
    <Page title={t("panel.title")} subtitle={t("ui.subtitle")}>
      <Stack>
        <Card title={t("ui.section.status")}>
          <Stack>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.label")}</Text>
              <Inline align="center" gap={8}>
                <StatusBadge
                  tone={enabled ? "success" : "default"}
                  label={t(enabled ? "ui.status.on" : "ui.status.off")}
                />
                <StatusBadge tone="default" label={state.switch_level || "L1"} />
              </Inline>
            </Inline>

            <Inline gap={12} wrap>
              <StatCard
                label={t("ui.stat.pending")}
                value={String(state.pending_count ?? pending.length)}
              />
              <StatCard
                label={t("ui.stat.authorized")}
                value={String(authorized.length)}
              />
              <StatCard
                label={t("ui.stat.tracked")}
                value={String(state.tracked_paths ?? 0)}
              />
            </Inline>

            {disableCount > 0 ? (
              <Inline align="center" gap={8} wrap>
                <StatusBadge tone="warning" label={t("ui.history.label")} />
                <Text>
                  {enabled && state.last_off_seconds
                    ? t("ui.history.wasOffFor").replace(
                        "{minutes}",
                        String(Math.max(1, Math.round(state.last_off_seconds / 60))),
                      )
                    : t("ui.history.count").replace(
                        "{count}",
                        String(disableCount),
                      )}
                </Text>
              </Inline>
            ) : null}

            <ButtonGroup>
              <Button
                tone="primary"
                disabled={busy || !canCheck}
                onClick={() => call("check_now", {}, t("ui.toast.checked"))}
              >
                {t("ui.action.checkNow")}
              </Button>
              <Button disabled={busy} onClick={() => props.api.refresh()}>
                {t("ui.action.refresh")}
              </Button>
            </ButtonGroup>

            {!canDecide ? (
              <Alert tone="info" message={t("ui.hint.startPlugin")} />
            ) : null}
          </Stack>
        </Card>

        <Card title={t("ui.section.pending")}>
          <Stack>
            {pending.length === 0 ? (
              <EmptyState
                title={t("ui.empty.pending.title")}
                description={t("ui.empty.pending.description")}
              />
            ) : (
              <List
                items={pending}
                render={(item: PendingItem) => (
                  <Card key={item.path}>
                    <Stack gap={6}>
                      <Inline align="center" justify="space-between">
                        <Text>{item.path}</Text>
                        <Inline align="center" gap={8}>
                          <StatusBadge
                            tone={levelTone(item.level)}
                            label={item.level}
                          />
                          {item.times_raised > 1 ? (
                            <StatusBadge
                              tone="warning"
                              label={t("ui.badge.repeated", {
                                count: item.times_raised,
                              })}
                            />
                          ) : null}
                        </Inline>
                      </Inline>
                      <Text>
                        {t("ui.change.summary", {
                          before: item.before,
                          after: item.after,
                        })}
                      </Text>
                      <Text>
                        {t("ui.change.raisedAt", {
                          when: formatTime(item.raised_at, t("ui.never")),
                        })}
                      </Text>
                      <Inline justify="end">
                        <ButtonGroup>
                          <Button
                            tone="warning"
                            disabled={busy || !canDecide}
                            onClick={() =>
                              call(
                                "keep_objecting",
                                { path: item.path },
                                t("ui.toast.objected"),
                              )
                            }
                          >
                            {t("ui.action.object")}
                          </Button>
                          <Button
                            tone="success"
                            disabled={busy || !canDecide}
                            onClick={() =>
                              call(
                                "accept_setting",
                                { path: item.path },
                                t("ui.toast.accepted"),
                              )
                            }
                          >
                            {t("ui.action.agree")}
                          </Button>
                        </ButtonGroup>
                      </Inline>
                    </Stack>
                  </Card>
                )}
              />
            )}
          </Stack>
        </Card>

        <Card title={t("ui.section.authorized")}>
          <Stack>
            {authorized.length === 0 ? (
              <EmptyState
                title={t("ui.empty.authorized.title")}
                description={t("ui.empty.authorized.description")}
              />
            ) : (
              <DataTable
                rowKey="path"
                data={authorized}
                columns={[
                  { key: "path", label: t("ui.column.path") },
                  {
                    key: "expires_at",
                    label: t("ui.column.expiresAt"),
                    render: (row: AuthorizedItem) =>
                      row.expires_at
                        ? formatTime(row.expires_at, t("ui.never"))
                        : t("ui.untilRevoked"),
                  },
                ]}
              />
            )}
          </Stack>
        </Card>

        <Card title={t("ui.section.switch")}>
          <Stack>
            <Alert tone="info" message={t("ui.switch.rule")} />
            {!enabled ? (
              <Button
                tone="success"
                disabled={busy || !canToggle}
                onClick={() =>
                  call(
                    "set_guard_enabled",
                    { enabled: true },
                    t("ui.toast.guardOn"),
                  )
                }
              >
                {t("ui.action.turnOn")}
              </Button>
            ) : pendingDisable ? (
              <Alert tone="warning" message={t("ui.switch.pending")} />
            ) : (
              <Button
                tone="danger"
                disabled={busy || !canToggle}
                onClick={() =>
                  call(
                    "set_guard_enabled",
                    { enabled: false },
                    t("ui.toast.disableRequested"),
                  )
                }
              >
                {t("ui.action.requestDisable")}
              </Button>
            )}
          </Stack>
        </Card>

        <Card title={t("ui.section.diagnostics")}>
          <Stack>
            <KeyValue
              items={[
                { label: t("ui.diag.baseUrl"), value: state.base_url || "" },
                { label: t("ui.diag.poll"), value: `${state.poll_seconds ?? 0}s` },
                {
                  label: t("ui.diag.rescan"),
                  value: `${state.full_rescan_seconds ?? 0}s`,
                },
                {
                  label: t("ui.diag.lastPoll"),
                  value: formatTime(state.last_poll_at, t("ui.never")),
                },
                {
                  label: t("ui.diag.revision"),
                  value:
                    state.revision === null || state.revision === undefined
                      ? t("ui.never")
                      : String(state.revision),
                },
              ]}
            />
            {state.last_error ? (
              <>
                <Divider />
                <Alert tone="danger" message={state.last_error} />
              </>
            ) : null}
          </Stack>
        </Card>

        <Tip>{t("ui.trust.note")}</Tip>
      </Stack>
    </Page>
  )
}
