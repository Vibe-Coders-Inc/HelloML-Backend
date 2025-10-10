-- Safety
DROP TABLE IF EXISTS public.business CASCADE;
DROP TABLE IF EXISTS public.agent CASCADE;
DROP TABLE IF EXISTS public.phone_number CASCADE;
DROP TABLE IF EXISTS public.document CASCADE;
DROP TABLE IF EXISTS public.document_chunk CASCADE;
DROP TABLE IF EXISTS public.conversation CASCADE;
DROP TABLE IF EXISTS public.message CASCADE;

-- ========== business ==========
CREATE TABLE IF NOT EXISTS public.business (
  id SERIAL PRIMARY KEY,
  owner_user_id UUID NOT NULL DEFAULT auth.uid(), 
  name TEXT NOT NULL,
  phone_number TEXT, -- actual business phone number for human fallback
  business_email TEXT, -- for emailing business, not the business owner 
  address TEXT NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ux_business_owner_name UNIQUE (owner_user_id, name)
);

-- ========== agent ==========
CREATE TABLE IF NOT EXISTS public.agent (
  id SERIAL PRIMARY KEY,
  business_id INT NOT NULL REFERENCES public.business(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT 'Agent', -- name of agent for funsies
  model_type TEXT NOT NULL DEFAULT 'gpt-5-nano',
  temperature  NUMERIC(3,2) NOT NULL DEFAULT 0.70 CHECK (temperature >= 0 AND temperature <= 2),
  voice_model TEXT DEFAULT 'Joanna',
  prompt TEXT,
  greeting TEXT DEFAULT 'Hello There!', -- obi wan reference
  goodbye TEXT DEFAULT 'Goodbye and take care!',
  status TEXT NOT NULL DEFAULT 'inactive' CHECK (status IN ('inactive','active','paused')), 
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- one agent per business
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_business ON public.agent(business_id);

-- ========== phone_number ==========
CREATE TABLE IF NOT EXISTS public.phone_number (
  id SERIAL PRIMARY KEY, 
  agent_id INT NOT NULL REFERENCES public.agent(id) ON DELETE CASCADE,
  phone_number TEXT NOT NULL,
  country TEXT NOT NULL,
  area_code TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'provisioning',
  webhook_url TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- unique phone and unique per agent
CREATE UNIQUE INDEX IF NOT EXISTS ux_phone_number_phone ON public.phone_number(phone_number);
CREATE UNIQUE INDEX IF NOT EXISTS ux_phone_number_agent ON public.phone_number(agent_id);

-- ========== pgvector ==========
CREATE EXTENSION IF NOT EXISTS vector;

-- ========== document (belongs to agent) ==========
CREATE TABLE IF NOT EXISTS public.document (
  id            SERIAL PRIMARY KEY,
  agent_id   INT NOT NULL REFERENCES public.agent(id) ON DELETE CASCADE, 
  filename      TEXT NOT NULL,                 
  storage_url  TEXT NOT NULL,                 -- path within the bucket
  file_type     TEXT,                          -- e.g., application/pdf, text/plain
  uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  error_message TEXT,     
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_document_agent_id  ON public.document(agent_id);
CREATE INDEX IF NOT EXISTS ix_document_uploaded_at  ON public.document(uploaded_at);

-- ========== document_chunk ==========
CREATE TABLE IF NOT EXISTS public.document_chunk (
  id SERIAL PRIMARY KEY,
  document_id INT NOT NULL REFERENCES public.document(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  chunk_text TEXT,
  embedding VECTOR(1536), 
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ux_document_chunk_idx UNIQUE (document_id, chunk_index)
);

-- ========== conversation ==========
CREATE TABLE public.conversation (
  id SERIAL PRIMARY KEY,
  agent_id INT NOT NULL REFERENCES public.agent(id) ON DELETE CASCADE,
  caller_phone TEXT,                       -- raw caller phone you receive from provider
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,                -- null while in-progress
  status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress','completed','failed','cancelled')) 
);

CREATE INDEX IF NOT EXISTS ix_conversation_agent    ON public.conversation(agent_id);
CREATE INDEX IF NOT EXISTS ix_conversation_started  ON public.conversation(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_conversation_status   ON public.conversation(status);


-- ========== message ==========
CREATE TABLE public.message (
  id               SERIAL PRIMARY KEY,
  conversation_id  INT NOT NULL REFERENCES public.conversation(id) ON DELETE CASCADE,
  role             TEXT NOT NULL CHECK (role IN ('user','agent','system')),   -- who spoke
  content          TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_message_conversation ON public.message(conversation_id);
CREATE INDEX IF NOT EXISTS ix_message_created      ON public.message(created_at);