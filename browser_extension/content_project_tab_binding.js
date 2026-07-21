"use strict";

const bdbBindProjectConversationBeforeTabBinding = bdbBindProjectConversation;

bdbBindProjectConversation = async function bdbBindProjectConversationWithTab(launch) {
  const storedLocally = await bdbBindProjectConversationBeforeTabBinding(launch);
  if (!storedLocally) {
    return false;
  }
  const conversationId = bdbProjectConversationId();
  if (!conversationId) {
    return false;
  }
  const result = await chrome.runtime.sendMessage({
    type: "BDB_SUBMIT_ACTION",
    action: {
      schema: ACTION_SCHEMA,
      operation: "project_conversation_bind",
      launch_id: launch.launch_id,
      conversation_id: conversationId,
      repo_alias: launch.repo_alias
    }
  });
  return Boolean(
    result &&
    result.ok === true &&
    result.response &&
    result.response.status === "conversation_bound" &&
    result.response.conversation_id === conversationId &&
    result.response.repo_alias === launch.repo_alias
  );
};
