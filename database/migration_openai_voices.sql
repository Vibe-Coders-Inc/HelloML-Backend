-- Migration: Update voice_model for OpenAI Realtime API voices
--
-- This migration updates the agent table to support OpenAI Realtime API voices
-- instead of Amazon Polly voices.
--
-- Valid OpenAI voices: alloy, echo, fable, onyx, nova, shimmer
-- Previous Polly voices: Joanna, Matthew, Amy, etc.

-- Update the default value for new agents
ALTER TABLE public.agent
ALTER COLUMN voice_model SET DEFAULT 'shimmer';

-- Add documentation comment
COMMENT ON COLUMN public.agent.voice_model IS
'Voice for OpenAI Realtime API. Valid options: alloy, echo, fable, onyx, nova, shimmer. Default: shimmer (soft, gentle tone).';

-- Update the model_type default for new agents
ALTER TABLE public.agent
ALTER COLUMN model_type SET DEFAULT 'gpt-realtime';

-- Add documentation comment for model_type
COMMENT ON COLUMN public.agent.model_type IS
'Model for AI agent. Use gpt-realtime for OpenAI Realtime API (speech-to-speech). Previous: gpt-5-nano (text only).';

-- OPTIONAL: Migrate existing agents from Polly voices to OpenAI voices
-- Uncomment the following if you want to automatically migrate existing agents:

/*
-- Map common Polly voices to similar OpenAI voices
UPDATE public.agent SET voice_model =
  CASE
    WHEN voice_model = 'Joanna' THEN 'nova'        -- Female, professional
    WHEN voice_model = 'Matthew' THEN 'onyx'       -- Male, professional
    WHEN voice_model = 'Amy' THEN 'shimmer'        -- Female, soft
    WHEN voice_model = 'Brian' THEN 'echo'         -- Male, confident
    WHEN voice_model = 'Emma' THEN 'alloy'         -- Neutral
    WHEN voice_model = 'Salli' THEN 'fable'        -- Expressive
    -- If already using OpenAI voice names, keep them
    WHEN voice_model IN ('alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer') THEN voice_model
    -- Default for any other Polly voices
    ELSE 'shimmer'
  END
WHERE voice_model IS NOT NULL;

-- Update model_type for all existing agents
UPDATE public.agent
SET model_type = 'gpt-realtime'
WHERE model_type = 'gpt-5-nano';
*/

-- Print summary (for PostgreSQL clients that support this)
DO $$
BEGIN
    RAISE NOTICE 'Migration complete: Updated voice_model defaults to OpenAI Realtime API voices';
    RAISE NOTICE 'Default voice is now "shimmer" (soft, gentle tone)';
    RAISE NOTICE 'Valid voices: alloy, echo, fable, onyx, nova, shimmer';
    RAISE NOTICE 'To migrate existing agents, uncomment the UPDATE statements in this file';
END $$;
