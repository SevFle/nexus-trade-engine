/**
 * BacktestPage — form to configure and submit a backtest run.
 *
 * The strategy dropdown is populated from `GET /api/v1/strategies/` via the
 * typed {@link apiClient} and TanStack Query. Submission fires a mutation
 * against `POST /api/v1/backtest/run` (synchronous kick-off that returns
 * `202 Accepted` with a `backtest_id`). Loading / error / success states are
 * first-class so a slow or absent backend degrades to an inline notice
 * instead of blanking the shell.
 *
 * Scope is intentionally tight: this page owns the form + submission only.
 * Results display (polling `GET /api/v1/backtest/results/{id}`) is a
 * follow-up cycle — on success we surface the returned `backtest_id` and a
 * pointer to the results route.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Play,
  RefreshCw,
} from "lucide-react";
import clsx from "clsx";

import {
  ApiError,
  apiClient,
  type BacktestSubmitRequest,
  type BacktestSubmitResponse,
} from "../lib/api";

// ---------------------------------------------------------------------------
// Strategy option normalization
// ---------------------------------------------------------------------------

/** A render-ready strategy dropdown option. */
interface StrategyOption {
  id: string;
  label: string;
}

/**
 * Normalize a single list entry into a dropdown option. The engine list
 * endpoint may return rich {@link StrategySummary}-shaped objects or bare
 * identifier strings (the minimal legacy registry); both are handled so the
 * dropdown never crashes on a schema mismatch.
 */
function toStrategyOption(raw: unknown, index: number): StrategyOption {
  if (typeof raw === "string" && raw.length > 0) {
    return { id: raw, label: raw };
  }
  const entry = (raw ?? {}) as Record<string, unknown>;
  const name =
    typeof entry.name === "string" && entry.name.length > 0
      ? (entry.name as string)
      : "";
  const hasId = typeof entry.id === "string" && entry.id.length > 0;
  // When the entry lacks a stable id, synthesize a composite key from the
  // name + position so siblings with identical names still produce unique
  // React keys / option values instead of colliding on a bare name.
  const id = hasId
    ? (entry.id as string)
    : name
      ? `${name}#${index}`
      : "";
  return { id, label: name || id };
}

// ---------------------------------------------------------------------------
// Cost model presets
// ---------------------------------------------------------------------------

/**
 * Cost model presets surfaced to the user. The selected token is passed
 * through to the engine under `config.cost_model`; the strategy/runner is
 * responsible for interpreting it. `default` maps to the engine's
 * {@link DefaultCostModel}; `zero` runs a frictionless baseline.
 */
interface CostModelOption {
  value: string;
  label: string;
  hint: string;
}

const COST_MODELS: CostModelOption[] = [
  { value: "default", label: "Default", hint: "Engine default commission + spread + slippage" },
  { value: "zero", label: "Frictionless", hint: "No costs — idealized baseline" },
  { value: "percentage", label: "Percentage", hint: "Percentage-of-notional cost model" },
  { value: "fixed", label: "Fixed", hint: "Flat per-trade commission" },
];

// ---------------------------------------------------------------------------
// Form state + validation
// ---------------------------------------------------------------------------

interface BacktestForm {
  strategy_name: string;
  symbol: string;
  start_date: string;
  end_date: string;
  initial_capital: string;
  cost_model: string;
}

const DEFAULT_FORM: BacktestForm = {
  strategy_name: "",
  symbol: "SPY",
  start_date: "",
  end_date: "",
  initial_capital: "100000",
  cost_model: "default",
};

type ValidationErrors = Partial<Record<keyof BacktestForm, string>>;

function validate(form: BacktestForm): ValidationErrors {
  const errors: ValidationErrors = {};
  if (!form.strategy_name) {
    errors.strategy_name = "Select a strategy.";
  }
  const symbol = form.symbol.trim().toUpperCase();
  if (!symbol) {
    errors.symbol = "Enter a ticker symbol.";
  }
  if (!form.start_date) {
    errors.start_date = "Choose a start date.";
  }
  if (!form.end_date) {
    errors.end_date = "Choose an end date.";
  }
  if (
    form.start_date &&
    form.end_date &&
    form.start_date > form.end_date
  ) {
    errors.end_date = "End date must be on or after the start date.";
  }
  // Validate the raw string with a strict decimal regex rather than
  // ``Number()``, which would happily coerce hex (``0x10``), scientific
  // (``1e5``) and whitespace-padded (``" 5 "``) inputs into valid numbers.
  if (
    !/^\d+(\.\d+)?$/.test(form.initial_capital) ||
    Number(form.initial_capital) <= 0
  ) {
    errors.initial_capital = "Initial capital must be a positive number.";
  }
  return errors;
}

/** Build the request body sent to `POST /api/v1/backtest/run`. */
function buildRequest(form: BacktestForm): BacktestSubmitRequest {
  return {
    strategy_name: form.strategy_name,
    symbol: form.symbol.trim().toUpperCase(),
    start_date: form.start_date,
    end_date: form.end_date,
    initial_capital: form.initial_capital,
    config: { cost_model: form.cost_model },
  };
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function BacktestPage() {
  const [form, setForm] = useState<BacktestForm>(DEFAULT_FORM);
  const [showErrors, setShowErrors] = useState(false);
  const [submitResult, setSubmitResult] =
    useState<BacktestSubmitResponse | null>(null);

  const {
    data: strategiesData,
    isLoading: strategiesLoading,
    isError: strategiesError,
    error: strategiesErr,
    refetch: refetchStrategies,
    isFetching: strategiesFetching,
  } = useQuery({
    queryKey: ["strategies", "list"],
    queryFn: () => apiClient.listStrategies(),
  });

  const options = useMemo<StrategyOption[]>(
    () =>
      (strategiesData?.strategies ?? [])
        .map(toStrategyOption)
        .filter((opt) => opt.id.length > 0)
        .sort((a, b) => a.label.localeCompare(b.label)),
    [strategiesData],
  );

  const mutation = useMutation({
    mutationFn: (req: BacktestSubmitRequest) => apiClient.runBacktest(req),
    onSuccess: (data) => setSubmitResult(data),
  });

  const errors = useMemo(() => validate(form), [form]);
  const isInvalid = Object.keys(errors).length > 0;

  function update<K extends keyof BacktestForm>(key: K, value: BacktestForm[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
    // Clear the previous success banner once the user edits anything.
    if (submitResult) setSubmitResult(null);
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mutation.isPending) return;
    setShowErrors(true);
    setSubmitResult(null);
    if (isInvalid) return;
    mutation.mutate(buildRequest(form));
  }

  return (
    <div className="p-xl text-nx-text-primary">
      <div className="mx-auto max-w-3xl">
        <header className="mb-3xl">
          <span className="mb-sm block font-mono text-label uppercase text-nx-text-secondary">
            Backtest Studio
          </span>
          <h1 className="font-display text-heading text-nx-text-display">
            Run a Backtest
          </h1>
          <p className="mt-xs font-mono text-caption text-nx-text-disabled">
            Configure a strategy, date range and capital, then submit a run.
            Results are computed asynchronously and tracked by backtest id.
          </p>
        </header>

        {strategiesError ? (
          <StrategyLoadError
            message={
              strategiesErr instanceof Error
                ? strategiesErr.message
                : "Failed to load strategies."
            }
            onRetry={() => refetchStrategies()}
          />
        ) : (
          <form
            onSubmit={handleSubmit}
            noValidate
            aria-label="Backtest configuration"
            data-testid="backtest-form"
            className="nx-card flex flex-col gap-xl"
          >
            {/* Strategy selection */}
            <Field
              id="strategy_name"
              label="Strategy"
              error={showErrors ? errors.strategy_name : undefined}
              hint={strategiesFetching ? "Refreshing list…" : undefined}
            >
              <select
                id="strategy_name"
                name="strategy_name"
                className="nx-input appearance-none"
                value={form.strategy_name}
                onChange={(e) => update("strategy_name", e.target.value)}
                disabled={strategiesLoading}
                aria-invalid={Boolean(showErrors && errors.strategy_name)}
                data-testid="backtest-strategy-select"
              >
                <option value="" disabled>
                  {strategiesLoading ? "Loading strategies…" : "Select a strategy"}
                </option>
                {options.map((opt) => (
                  <option key={opt.id} value={opt.id}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </Field>

            <div className="grid grid-cols-1 gap-xl md:grid-cols-2">
              {/* Symbol */}
              <Field
                id="symbol"
                label="Symbol"
                error={showErrors ? errors.symbol : undefined}
              >
                <input
                  id="symbol"
                  name="symbol"
                  type="text"
                  className="nx-input"
                  placeholder="e.g. AAPL"
                  value={form.symbol}
                  onChange={(e) => update("symbol", e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={Boolean(showErrors && errors.symbol)}
                  data-testid="backtest-symbol-input"
                />
              </Field>

              {/* Initial capital */}
              <Field
                id="initial_capital"
                label="Initial Capital (USD)"
                error={showErrors ? errors.initial_capital : undefined}
              >
                <input
                  id="initial_capital"
                  name="initial_capital"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step="0.01"
                  className="nx-input"
                  placeholder="100000"
                  value={form.initial_capital}
                  onChange={(e) => update("initial_capital", e.target.value)}
                  aria-invalid={Boolean(showErrors && errors.initial_capital)}
                  data-testid="backtest-capital-input"
                />
              </Field>

              {/* Start date */}
              <Field
                id="start_date"
                label="Start Date"
                error={showErrors ? errors.start_date : undefined}
              >
                <input
                  id="start_date"
                  name="start_date"
                  type="date"
                  className="nx-input"
                  value={form.start_date}
                  onChange={(e) => update("start_date", e.target.value)}
                  aria-invalid={Boolean(showErrors && errors.start_date)}
                  data-testid="backtest-start-date"
                />
              </Field>

              {/* End date */}
              <Field
                id="end_date"
                label="End Date"
                error={showErrors ? errors.end_date : undefined}
              >
                <input
                  id="end_date"
                  name="end_date"
                  type="date"
                  className="nx-input"
                  value={form.end_date}
                  onChange={(e) => update("end_date", e.target.value)}
                  aria-invalid={Boolean(showErrors && errors.end_date)}
                  data-testid="backtest-end-date"
                />
              </Field>
            </div>

            {/* Cost model */}
            <Field
              id="cost_model"
              label="Cost Model"
              hint={
                COST_MODELS.find((m) => m.value === form.cost_model)?.hint
              }
            >
              <select
                id="cost_model"
                name="cost_model"
                className="nx-input appearance-none"
                value={form.cost_model}
                onChange={(e) => update("cost_model", e.target.value)}
                data-testid="backtest-cost-model-select"
              >
                {COST_MODELS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </Field>

            <SubmissionControls
              isPending={mutation.isPending}
              disabled={strategiesLoading}
              onReset={() => {
                setForm(DEFAULT_FORM);
                setShowErrors(false);
                setSubmitResult(null);
                mutation.reset();
              }}
            />

            {mutation.isError && (
              <SubmitError
                message={
                  mutation.error instanceof Error
                    ? mutation.error.message
                    : "Backtest submission failed."
                }
                isConsentRequired={
                  mutation.error instanceof ApiError &&
                  mutation.error.isConsentRequired
                }
                isAuthError={
                  mutation.error instanceof ApiError && mutation.error.isAuthError
                }
              />
            )}

            {submitResult && (
              <SubmitSuccess
                result={submitResult}
                form={form}
                options={options}
              />
            )}
          </form>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Field wrapper
// ---------------------------------------------------------------------------

interface FieldProps {
  id: string;
  label: string;
  error?: string;
  hint?: string;
  children: React.ReactNode;
}

function Field({ id, label, error, hint, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-2xs">
      <label htmlFor={id} className="nx-label">
        {label}
      </label>
      {children}
      {error ? (
        <span
          role="alert"
          className="font-mono text-caption text-nx-accent"
          data-testid={`${id}-error`}
        >
          {error}
        </span>
      ) : hint ? (
        <span className="font-mono text-caption text-nx-text-disabled">
          {hint}
        </span>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Submission controls
// ---------------------------------------------------------------------------

interface SubmissionControlsProps {
  isPending: boolean;
  disabled: boolean;
  onReset: () => void;
}

function SubmissionControls({
  isPending,
  disabled,
  onReset,
}: SubmissionControlsProps) {
  return (
    <div className="flex items-center gap-md">
      <button
        type="submit"
        className="nx-btn-primary"
        disabled={isPending || disabled}
        data-testid="backtest-submit"
      >
        {isPending ? (
          <Loader2 size={14} strokeWidth={1.75} className="mr-xs animate-spin" />
        ) : (
          <Play size={14} strokeWidth={1.75} className="mr-xs" />
        )}
        {isPending ? "Submitting…" : "Run Backtest"}
      </button>
      <button
        type="button"
        onClick={onReset}
        className="nx-btn-secondary"
        disabled={isPending}
        data-testid="backtest-reset"
      >
        Reset
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

interface StrategyLoadErrorProps {
  message: string;
  onRetry: () => void;
}

function StrategyLoadError({ message, onRetry }: StrategyLoadErrorProps) {
  return (
    <section
      role="alert"
      aria-label="Strategies load error"
      data-testid="backtest-strategies-error"
      className="flex flex-col items-start gap-md rounded-lg border border-nx-accent/40 bg-nx-surface-raised p-lg"
    >
      <div className="flex items-center gap-xs text-nx-accent">
        <AlertTriangle size={16} strokeWidth={1.75} />
        <span className="font-mono text-label uppercase tracking-wider">
          Couldn&apos;t load strategies
        </span>
      </div>
      <p className="font-mono text-body-sm text-nx-text-secondary">{message}</p>
      <button type="button" onClick={onRetry} className="nx-btn-secondary">
        <RefreshCw size={14} strokeWidth={1.75} className="mr-xs" />
        Retry
      </button>
    </section>
  );
}

interface SubmitErrorProps {
  message: string;
  isConsentRequired: boolean;
  isAuthError: boolean;
}

function SubmitError({
  message,
  isConsentRequired,
  isAuthError,
}: SubmitErrorProps) {
  const hint = isConsentRequired
    ? "Accept the latest legal documents and try again."
    : isAuthError
      ? "Your session may have expired — sign in again."
      : null;
  return (
    <section
      role="alert"
      aria-label="Backtest submission error"
      data-testid="backtest-submit-error"
      className="flex flex-col items-start gap-xs rounded-lg border border-nx-accent/40 bg-nx-surface-raised p-md"
    >
      <div className="flex items-center gap-xs text-nx-accent">
        <AlertTriangle size={14} strokeWidth={1.75} />
        <span className="font-mono text-label uppercase tracking-wider">
          Submission failed
        </span>
      </div>
      <p className="font-mono text-body-sm text-nx-text-secondary">{message}</p>
      {hint && (
        <p className="font-mono text-caption text-nx-text-disabled">{hint}</p>
      )}
    </section>
  );
}

interface SubmitSuccessProps {
  result: BacktestSubmitResponse;
  form: BacktestForm;
  options: StrategyOption[];
}

function SubmitSuccess({ result, form, options }: SubmitSuccessProps) {
  const strategyLabel =
    options.find((o) => o.id === form.strategy_name)?.label ??
    form.strategy_name;
  return (
    <section
      role="status"
      aria-live="polite"
      aria-label="Backtest submitted"
      data-testid="backtest-submit-success"
      className="flex flex-col gap-sm rounded-lg border border-nx-success/40 bg-nx-success/5 p-md"
    >
      <div className="flex items-center gap-xs text-nx-success">
        <CheckCircle2 size={16} strokeWidth={1.75} />
        <span className="font-mono text-label uppercase tracking-wider">
          Backtest submitted
        </span>
      </div>
      <div className="grid grid-cols-1 gap-xs font-mono text-body-sm text-nx-text-secondary sm:grid-cols-2">
        <SuccessRow label="Backtest ID" value={result.backtest_id || "—"} mono />
        <SuccessRow label="Status" value={result.status || "accepted"} />
        <SuccessRow label="Strategy" value={strategyLabel} />
        <SuccessRow label="Symbol" value={form.symbol.trim().toUpperCase()} />
        <SuccessRow
          label="Period"
          value={`${form.start_date} → ${form.end_date}`}
        />
        <SuccessRow
          label="Capital"
          value={`$${Number(form.initial_capital).toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}`}
        />
      </div>
      <p className="font-mono text-caption text-nx-text-disabled">
        The engine processes runs asynchronously. Use the Backtest ID to look
        up results once complete.
      </p>
    </section>
  );
}

interface SuccessRowProps {
  label: string;
  value: string;
  mono?: boolean;
}

function SuccessRow({ label, value, mono }: SuccessRowProps) {
  return (
    <div className="flex flex-col gap-2xs">
      <span className="text-caption uppercase tracking-wider text-nx-text-disabled">
        {label}
      </span>
      <span
        className={clsx(
          "break-all text-nx-text-primary",
          mono && "font-mono text-caption",
        )}
      >
        {value}
      </span>
    </div>
  );
}
