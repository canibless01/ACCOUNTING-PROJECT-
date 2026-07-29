-- ================================================================
-- HOLY GRILLS — SCHEMA MIGRATION v6
-- Transfer Detection Rules
-- ================================================================
-- Run this in your Supabase SQL Editor (after v5 migration).
-- Safe to re-run — uses IF NOT EXISTS throughout.
-- ================================================================

-- ── Transfer rules table ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.transfer_rules (
  id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  -- Optional friendly label (e.g. "UBA → OPay")
  name                 TEXT,
  -- Which account the debit comes from (NULL = any account)
  from_account_id      UUID        REFERENCES public.accounts(id) ON DELETE SET NULL,
  -- Substring match on the debit narration (case-insensitive)
  debit_pattern        TEXT        NOT NULL,
  -- Which account the credit goes to (NULL = any account)
  to_account_id        UUID        REFERENCES public.accounts(id) ON DELETE SET NULL,
  -- Substring match on the credit narration (case-insensitive)
  credit_pattern       TEXT        NOT NULL,
  -- How far apart (in minutes) the debit and credit can be and still be linked
  time_window_minutes  INTEGER     NOT NULL DEFAULT 60 CHECK (time_window_minutes > 0),
  is_active            BOOLEAN     NOT NULL DEFAULT TRUE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RLS ───────────────────────────────────────────────────────────────────────
ALTER TABLE public.transfer_rules ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  CREATE POLICY "Users manage own transfer rules"
    ON public.transfer_rules FOR ALL
    USING  (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_transfer_rules_user_active
  ON public.transfer_rules (user_id, is_active);

-- ── updated_at trigger (reuse existing helper if present) ────────────────────
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc
    WHERE proname = 'set_updated_at'
    AND pronamespace = 'public'::regnamespace
  ) THEN
    EXECUTE $t$
      CREATE TRIGGER transfer_rules_updated_at
        BEFORE UPDATE ON public.transfer_rules
        FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
    $t$;
  END IF;
EXCEPTION WHEN duplicate_object THEN NULL; END;
$$;
