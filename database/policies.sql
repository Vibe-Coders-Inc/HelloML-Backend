-- ========== Enable RLS ==========
ALTER TABLE public.business         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.phone_number     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_chunk   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversation     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.message          ENABLE ROW LEVEL SECURITY;

-- ========== BUSINESS ==========
-- (owned directly by auth.uid())
DROP POLICY IF EXISTS biz_select_own  ON public.business;
DROP POLICY IF EXISTS biz_insert_self ON public.business;
DROP POLICY IF EXISTS biz_update_own  ON public.business;
DROP POLICY IF EXISTS biz_delete_own  ON public.business;

-- Read only your own businesses
CREATE POLICY biz_select_own
ON public.business
FOR SELECT
TO authenticated
USING (owner_user_id = auth.uid());

-- Insert businesses for yourself
CREATE POLICY biz_insert_self
ON public.business
FOR INSERT
TO authenticated
WITH CHECK (owner_user_id = auth.uid());

-- Update only your own
CREATE POLICY biz_update_own
ON public.business
FOR UPDATE
TO authenticated
USING (owner_user_id = auth.uid())
WITH CHECK (owner_user_id = auth.uid());

-- Delete only your own
CREATE POLICY biz_delete_own
ON public.business
FOR DELETE
TO authenticated
USING (owner_user_id = auth.uid());

-- ========== AGENT ==========
-- (belongs to a business you own)
DROP POLICY IF EXISTS agent_select_own_business  ON public.agent;
DROP POLICY IF EXISTS agent_insert_own_business  ON public.agent;
DROP POLICY IF EXISTS agent_update_own_business  ON public.agent;
DROP POLICY IF EXISTS agent_delete_own_business  ON public.agent;

-- Select agents whose business you own
CREATE POLICY agent_select_own_business
ON public.agent
FOR SELECT
TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.business b
    WHERE b.id = agent.business_id
      AND b.owner_user_id = auth.uid()
  )
);

-- Insert agents only for businesses you own
CREATE POLICY agent_insert_own_business
ON public.agent
FOR INSERT
TO authenticated
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.business b
    WHERE b.id = agent.business_id
      AND b.owner_user_id = auth.uid()
  )
);

-- Update agents only if they belong to your business
CREATE POLICY agent_update_own_business
ON public.agent
FOR UPDATE
TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.business b
    WHERE b.id = agent.business_id
      AND b.owner_user_id = auth.uid()
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.business b
    WHERE b.id = agent.business_id
      AND b.owner_user_id = auth.uid()
  )
);

-- Delete agents only if they belong to your business
CREATE POLICY agent_delete_own_business
ON public.agent
FOR DELETE
TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.business b
    WHERE b.id = agent.business_id
      AND b.owner_user_id = auth.uid()
  )
);

-- ========== PHONE NUMBER ==========
-- (belongs to an agent in your business)
DROP POLICY IF EXISTS phone_select_own_agent_business ON public.phone_number;
DROP POLICY IF EXISTS phone_insert_own_agent_business ON public.phone_number;
DROP POLICY IF EXISTS phone_update_own_agent_business ON public.phone_number;
DROP POLICY IF EXISTS phone_delete_own_agent_business ON public.phone_number;

CREATE POLICY phone_select_own_agent_business
ON public.phone_number
FOR SELECT
TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = phone_number.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY phone_insert_own_agent_business
ON public.phone_number
FOR INSERT
TO authenticated
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = phone_number.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY phone_update_own_agent_business
ON public.phone_number
FOR UPDATE
TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = phone_number.agent_id
      AND b.owner_user_id = auth.uid()
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = phone_number.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY phone_delete_own_agent_business
ON public.phone_number
FOR DELETE
TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = phone_number.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

-- ==========DOCUMENT ==========
-- (belongs to an agent in your business)
DROP POLICY IF EXISTS doc_select_own   ON public.document;
DROP POLICY IF EXISTS doc_insert_own   ON public.document;
DROP POLICY IF EXISTS doc_update_own   ON public.document;
DROP POLICY IF EXISTS doc_delete_own   ON public.document;

CREATE POLICY doc_select_own
ON public.document
FOR SELECT TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = document.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY doc_insert_own
ON public.document
FOR INSERT TO authenticated
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = document.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY doc_update_own
ON public.document
FOR UPDATE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = document.agent_id
      AND b.owner_user_id = auth.uid()
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = document.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY doc_delete_own
ON public.document
FOR DELETE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = document.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

-- ========== DOCUMENT CHUNK ==========
-- (via document -> agent -> business ownership)
DROP POLICY IF EXISTS chunk_select_own_document ON public.document_chunk;
DROP POLICY IF EXISTS chunk_insert_own_document ON public.document_chunk;
DROP POLICY IF EXISTS chunk_update_own_document ON public.document_chunk;
DROP POLICY IF EXISTS chunk_delete_own_document ON public.document_chunk;

CREATE POLICY chunk_select_own_document
ON public.document_chunk
FOR SELECT TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.document d
    JOIN public.agent a      ON a.id = d.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE d.id = document_chunk.document_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY chunk_insert_own_document
ON public.document_chunk
FOR INSERT TO authenticated
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.document d
    JOIN public.agent a      ON a.id = d.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE d.id = document_chunk.document_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY chunk_update_own_document
ON public.document_chunk
FOR UPDATE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.document d
    JOIN public.agent a      ON a.id = d.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE d.id = document_chunk.document_id
      AND b.owner_user_id = auth.uid()
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.document d
    JOIN public.agent a      ON a.id = d.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE d.id = document_chunk.document_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY chunk_delete_own_document
ON public.document_chunk
FOR DELETE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.document d
    JOIN public.agent a      ON a.id = d.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE d.id = document_chunk.document_id
      AND b.owner_user_id = auth.uid()
  )
);

-- ========== CONVERSATION ==========
-- (belongs to an agent in your business)
DROP POLICY IF EXISTS conv_select_own_business ON public.conversation;
DROP POLICY IF EXISTS conv_insert_own_business ON public.conversation;
DROP POLICY IF EXISTS conv_update_own_business ON public.conversation;
DROP POLICY IF EXISTS conv_delete_own_business ON public.conversation;

CREATE POLICY conv_select_own_business
ON public.conversation
FOR SELECT TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = conversation.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY conv_insert_own_business
ON public.conversation
FOR INSERT TO authenticated
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = conversation.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY conv_update_own_business
ON public.conversation
FOR UPDATE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = conversation.agent_id
      AND b.owner_user_id = auth.uid()
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = conversation.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY conv_delete_own_business
ON public.conversation
FOR DELETE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.agent a
    JOIN public.business b ON b.id = a.business_id
    WHERE a.id = conversation.agent_id
      AND b.owner_user_id = auth.uid()
  )
);

-- ========== MESSAGE ==========
-- (belongs to conversation -> agent -> your business)
DROP POLICY IF EXISTS msg_select_own_conversation ON public.message;
DROP POLICY IF EXISTS msg_insert_own_conversation ON public.message;
DROP POLICY IF EXISTS msg_update_own_conversation ON public.message;
DROP POLICY IF EXISTS msg_delete_own_conversation ON public.message;

CREATE POLICY msg_select_own_conversation
ON public.message
FOR SELECT TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.conversation c
    JOIN public.agent a      ON a.id = c.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE c.id = message.conversation_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY msg_insert_own_conversation
ON public.message
FOR INSERT TO authenticated
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.conversation c
    JOIN public.agent a      ON a.id = c.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE c.id = message.conversation_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY msg_update_own_conversation
ON public.message
FOR UPDATE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.conversation c
    JOIN public.agent a      ON a.id = c.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE c.id = message.conversation_id
      AND b.owner_user_id = auth.uid()
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1
    FROM public.conversation c
    JOIN public.agent a      ON a.id = c.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE c.id = message.conversation_id
      AND b.owner_user_id = auth.uid()
  )
);

CREATE POLICY msg_delete_own_conversation
ON public.message
FOR DELETE TO authenticated
USING (
  EXISTS (
    SELECT 1
    FROM public.conversation c
    JOIN public.agent a      ON a.id = c.agent_id
    JOIN public.business b   ON b.id = a.business_id
    WHERE c.id = message.conversation_id
      AND b.owner_user_id = auth.uid()
  )
);