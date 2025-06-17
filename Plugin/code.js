// =========================================================================
// AI Design Assistant - Figma Plugin Code (Backend + Auth Version)
// =========================================================================

figma.showUI(__html__, {
  width: 450,
  height: 600, // Adjusted height for auth section
  title: "AI Design Assistant",
});

// --- Backend URLs (Moved from ui.html) ---
const BACKEND_URL = "http://localhost:5001/generate";
const TOKEN_EXCHANGE_URL = "http://localhost:5001/auth/exchange-id-token-for-custom-token"; // Used by UI, but here for context
const SET_API_KEY_URL = "http://localhost:5001/auth/set-api-key";

// State variables
let lastNotifiedFrameId = null;
let lastNotifiedMode = null;
let originalSelectedNodeId = null; // Still needed for modify replacement
let isProcessing = false; // Use this flag to prevent selection changes during AI process

// New state variables for authentication, managed by messages from UI
let currentUserIdToken = null;
let isUserAuthenticated = false;
let hasUserProvidedApiKey = false;

// findTopLevelFrame function (remains the same)
function findTopLevelFrame(node) {
  let current = node;
  if (!current) return null;
  if (
    current.type === "FRAME" &&
    current.parent &&
    current.parent.type === "PAGE"
  ) {
    return current;
  }
  let parent = current.parent;
  while (parent) {
    if (
      parent.type === "FRAME" &&
      parent.parent &&
      parent.parent.type === "PAGE"
    ) {
      return parent;
    }
    if (parent.type === "PAGE") {
      return null;
    }
    parent = parent.parent;
  }
  return null;
}

// =========================================================================
// CORRECTED: Convert Uint8Array to Base64 using a pure JS implementation
// (Since Blob and btoa are not available in Figma's code.js)
// =========================================================================
const _uint8ToBase64Chars =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";

function uint8ArrayToBase64(bytes) {
  let result = "";
  let i;
  const len = bytes.length;

  for (i = 0; i < len; i += 3) {
    const b1 = bytes[i];
    const b2 = bytes[i + 1];
    const b3 = bytes[i + 2];

    // Group 3 bytes into 4 Base64 characters
    // First character
    const enc1 = b1 >> 2;
    // Second character
    const enc2 = ((b1 & 3) << 4) | (b2 >> 4);
    // Third character
    const enc3 = ((b2 & 15) << 2) | (b3 >> 6);
    // Fourth character
    const enc4 = b3 & 63;

    result +=
      _uint8ToBase64Chars.charAt(enc1) + _uint8ToBase64Chars.charAt(enc2);

    // Handle padding for remaining bytes (less than 3)
    if (isNaN(b2)) {
      // Only 1 byte left
      result += "==";
    } else if (isNaN(b3)) {
      // Only 2 bytes left
      result += _uint8ToBase64Chars.charAt(enc3) + "=";
    } else {
      // 3 bytes, no padding
      result +=
        _uint8ToBase64Chars.charAt(enc3) + _uint8ToBase64Chars.charAt(enc4);
    }
  }
  return result;
}
// =========================================================================

// --- Selection Change Handler ---
figma.on("selectionchange", async () => {
  if (isProcessing) {
    return;
  }
  // New: Check if user is authenticated before allowing selection processing
  if (!isUserAuthenticated) {
    figma.ui.postMessage({
      type: "selection-invalid",
      reason: "Please sign in to use the assistant.",
    });
    lastNotifiedFrameId = null;
    lastNotifiedMode = "answer";
    return;
  }

  const selection = figma.currentPage.selection;
  let mode = "answer";
  let frameId = null;
  let frameName = null;
  let elementInfo = null;
  originalSelectedNodeId = null;

  if (selection.length !== 1) {
    figma.ui.postMessage({
      type: "selection-invalid",
      reason: "Please select exactly one item.",
    });
    lastNotifiedFrameId = null;
    lastNotifiedMode = "answer";
    return;
  }

  const selectedNode = selection[0];
  if (selectedNode.type === "PAGE") {
    figma.ui.postMessage({
      type: "selection-invalid",
      reason: "Please select a frame or an element, not the page.",
    });
    lastNotifiedFrameId = null;
    lastNotifiedMode = "answer";
    return;
  }

  const targetFrame = findTopLevelFrame(selectedNode);

  if (!targetFrame) {
    if (
      selectedNode.type === "FRAME" &&
      selectedNode.parent &&
      selectedNode.parent.type === "PAGE"
    ) {
      if (selectedNode.children.length === 0) {
        mode = "create";
        frameId = selectedNode.id;
        frameName = selectedNode.name;
      } else {
        figma.ui.postMessage({
          type: "selection-invalid",
          reason:
            "Select element *inside* frame to modify, or an *empty* frame to create.",
        });
        lastNotifiedFrameId = null;
        lastNotifiedMode = "answer";
        return;
      }
    } else {
      figma.ui.postMessage({
        type: "selection-invalid",
        reason: "Selected item must be within a top-level frame.",
      });
      lastNotifiedFrameId = null;
      lastNotifiedMode = "answer";
      return;
    }
  } else {
    frameId = targetFrame.id;
    frameName = targetFrame.name;

    if (
      selectedNode.id === targetFrame.id &&
      targetFrame.children.length === 0
    ) {
      mode = "create";
    } else if (selectedNode.id !== targetFrame.id && selectedNode.parent) {
      mode = "modify";
      originalSelectedNodeId = selectedNode.id;
      elementInfo = {
        id: selectedNode.id,
        name: selectedNode.name,
        type: selectedNode.type,
        width: selectedNode.width,
        height: selectedNode.height,
      };
    } else if (
      selectedNode.id === targetFrame.id &&
      targetFrame.children.length > 0
    ) {
      figma.ui.postMessage({
        type: "selection-invalid",
        reason:
          "Select element *inside* frame to modify, or an *empty* frame to create.",
      });
      lastNotifiedFrameId = null;
      lastNotifiedMode = "answer";
      return;
    } else {
      figma.ui.postMessage({
        type: "selection-invalid",
        reason: "Invalid selection. Ensure item is in a top-level frame.",
      });
      lastNotifiedFrameId = null;
      lastNotifiedMode = "answer";
      return;
    }
  }

  lastNotifiedFrameId = frameId;
  lastNotifiedMode = mode;

  figma.ui.postMessage({
    type: "selection-update",
    mode: mode,
    frameId: frameId,
    frameName: frameName,
    element: elementInfo,
  });
});

// --- Message Handling from UI ---
figma.ui.onmessage = async (msg) => {
  console.log("Message received from ui.html:", msg.type);

  // New: Handle authentication state updates from UI
  if (msg.type === "auth-state-changed") {
    currentUserIdToken = msg.idToken;
    isUserAuthenticated = msg.isAuthenticated;
    hasUserProvidedApiKey = msg.hasApiKey;
    console.log(
      `Code.js received auth-state-changed. Authenticated: ${isUserAuthenticated}, Has API Key: ${hasUserProvidedApiKey}`
    );
    // Re-trigger selection change to update UI based on auth status
    // figma.trigger('selectionchange');
    return; // Important to return here to avoid processing as a different message type
  }
  // New: Handle request to set API key from UI
  else if (msg.type === "request-set-api-key") {
    const { apiKey } = msg;

    if (!currentUserIdToken) {
      figma.ui.postMessage({
        type: "backend-api-key-status",
        success: false,
        error: "Authentication token missing. Please sign in to save your key.",
      });
      return;
    }

    figma.ui.postMessage({
      type: "status-update",
      text: "Saving API key...",
      isLoading: true,
    });

    try {
      const response = await fetch(SET_API_KEY_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${currentUserIdToken}`,
        },
        body: JSON.stringify({ apiKey: apiKey }),
      });

      const result = await response.json();
      if (response.ok && result.success) {
        hasUserProvidedApiKey = true; // Update state in code.js
        figma.ui.postMessage({
          type: "backend-api-key-status",
          success: true,
          message: result.message,
        });
      } else {
        let errorMsg = result.error || "Failed to save key.";
        if (response.status === 401) {
          errorMsg = `Authentication failed: ${errorMsg}. Please sign in again.`;
          figma.ui.postMessage({
            type: "backend-response-error",
            status: 401,
            error: errorMsg,
          }); // Signal UI to sign out
        } else {
          figma.ui.postMessage({
            type: "backend-api-key-status",
            success: false,
            error: errorMsg,
          });
        }
      }
    } catch (error) {
      console.error("Error sending API key to backend from code.js:", error);
      figma.ui.postMessage({
        type: "backend-api-key-status",
        success: false,
        error: `Network/Connection Error: ${error.message}`,
      });
    } finally {
      // UI will handle setLoading(false) upon receiving backend-api-key-status
    }
  }
  // --- Request from UI to START AI Generation (Initial trigger after auth/prompt) ---
  else if (msg.type === "request-ai-generation") {
    isProcessing = true; // Set processing flag
    const { mode, userPrompt, elementInfo } = msg; // elementInfo is passed directly from UI
    let figmaFrameId = null; // Renamed to avoid confusion with `frameId` from msg.context

    // Crucial: Check for authentication token before proceeding
    if (!currentUserIdToken) {
      figma.ui.postMessage({
        type: "backend-response-error",
        status: 401,
        error:
          "Authentication token missing. Please sign in to use the assistant.",
      });
      isProcessing = false;
      return;
    }

    try {
      figma.ui.postMessage({
        type: "status-update",
        text: `Preparing "${mode}" request...`,
        isLoading: true,
      });

      // Re-validate selection to ensure it's still valid
      const selection = figma.currentPage.selection;
      if (selection.length !== 1) {
        throw new Error("Invalid selection: Please select exactly one item.");
      }
      const selectedNode = selection[0];
      const targetFrame = findTopLevelFrame(selectedNode);

      // Determine the actual frameId and frameName from Figma's live selection
      // This logic is duplicated but necessary to ensure the node is still valid.
      let context = { frameName: null };
      if (
        selectedNode.type === "FRAME" &&
        selectedNode.parent &&
        selectedNode.parent.type === "PAGE"
      ) {
        figmaFrameId = selectedNode.id;
        context.frameName = selectedNode.name;
      } else if (targetFrame) {
        figmaFrameId = targetFrame.id;
        context.frameName = targetFrame.name;
      } else {
        if (mode !== "answer") {
          // Only require a frame for create/modify
          throw new Error(
            "Selected item must be within a top-level frame or be a top-level frame itself."
          );
        }
      }

      if (!figmaFrameId && mode !== "answer") {
        throw new Error(
          `Internal Error: Frame ID is missing for mode "${mode}". Please reselect.`
        );
      }

      // Construct payload for the backend
      const backendPayload = {
        mode: mode,
        userPrompt: userPrompt,
        context: context,
      };

      let originalElementId = null; // To store for replacement

      if (mode === "modify") {
        if (!elementInfo || !elementInfo.id) {
          throw new Error(
            "Missing element information for modification. Please reselect."
          );
        }
        const elementToModify = await figma.getNodeByIdAsync(elementInfo.id);
        if (!elementToModify || elementToModify.removed) {
          throw new Error(
            `The selected element (ID: ${elementInfo.id}) seems to have been removed. Please reselect.`
          );
        }

        context["elementInfo"] = elementInfo; // Add element info to context
        backendPayload.context = context; // Ensure updated context is used
        originalElementId = elementInfo.id; // Store original ID for Figma action later

        figma.ui.postMessage({
          type: "status-update",
          text: `Exporting frame "${context.frameName}" and element for analysis...`,
          isLoading: true,
        });
        figma.notify(`⏳ Exporting frame "${context.frameName}"...`);

        try {
          const exportSettings = {
            format: "PNG",
            constraint: { type: "SCALE", value: 1 },
          };
          const [framePngBytes, elementPngBytes] = await Promise.all([
            targetFrame ? targetFrame.exportAsync(exportSettings) : null, // Ensure targetFrame exists for export
            elementToModify.exportAsync(exportSettings),
          ]);

          // Call the corrected synchronous uint8ArrayToBase64 function
          backendPayload.frameDataBase64 = framePngBytes
            ? uint8ArrayToBase64(framePngBytes)
            : null;
          backendPayload.elementDataBase64 = elementPngBytes
            ? uint8ArrayToBase64(elementPngBytes)
            : null;
        } catch (error) {
          throw new Error(`Export Error: ${error.message || "Unknown error"}`);
        }
      } else if (mode === "create") {
        if (!figmaFrameId) {
          throw new Error(
            "Internal Error: Target frame not available for creation."
          );
        }
        const targetFrameNode = await figma.getNodeByIdAsync(figmaFrameId);
        if (
          !targetFrameNode ||
          targetFrameNode.removed ||
          targetFrameNode.type !== "FRAME"
        ) {
          throw new Error(
            `Target frame (ID: ${figmaFrameId}) not found or invalid. Please reselect.`
          );
        }
        if (targetFrameNode.children.length > 0) {
          throw new Error(
            `Target frame "${targetFrameNode.name}" is not empty. Cannot create new design.`
          );
        }
        // No images needed for create mode, backendPayload is already ready
      } else if (mode === "answer") {
        // No specific Figma API calls or image exports for answer mode
      } else {
        throw new Error(
          `Internal Error: Unknown mode "${mode}" in request-ai-generation message.`
        );
      }

      // --- CALL BACKEND API DIRECTLY FROM CODE.JS ---
      figma.ui.postMessage({
        type: "status-update",
        text: "Communicating with AI Assistant Backend...",
        isLoading: true,
      });

      const response = await fetch(BACKEND_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${currentUserIdToken}`, // Use the stored ID token
        },
        body: JSON.stringify(backendPayload),
      });

      const result = await response.json();

      // Handle backend response status and result
      if (!response.ok || !result.success) {
        let errorMsg = result.error || "Backend returned an unspecified error.";
        // Specific handling for authentication errors from backend
        if (response.status === 401) {
          errorMsg = `Authentication failed: ${errorMsg}. Please sign in again.`;
          // Signal UI to clear session and prompt re-authentication
          figma.ui.postMessage({
            type: "backend-response-error",
            status: 401,
            error: errorMsg,
          });
        } else {
          // For other backend errors (e.g., trial expired)
          figma.ui.postMessage({
            type: "backend-response-error",
            status: response.status,
            error: errorMsg,
          });
          // If it was a trial expired error, ensure the banner is updated by UI
          if (result.mode === "trial_expired") {
            hasUserProvidedApiKey = false; // Reflect this state in code.js, UI will also update.
            // Force UI banner update (via auth-state-changed) to show "unlock unlimited"
            // This message should primarily come from onAuthStateChanged in UI after backend `hasApiKey` status update
            // figma.ui.postMessage({ type: 'auth-state-changed', idToken: currentUserIdToken, isAuthenticated: isUserAuthenticated, hasApiKey: hasUserProvidedApiKey, email: null });
          }
        }
        isProcessing = false;
        return;
      }

      // Backend call successful, now perform Figma actions based on result
      if (result.mode === "svg") {
        if (mode === "create") {
          const targetFrameNode = await figma.getNodeByIdAsync(figmaFrameId);
          if (
            !targetFrameNode ||
            targetFrameNode.removed ||
            targetFrameNode.type !== "FRAME"
          ) {
            throw new Error(
              `Target frame (ID: ${figmaFrameId}) not found or invalid for replacement. Please reselect.`
            );
          }

          figma.ui.postMessage({
            type: "status-update",
            text: "Importing generated SVG...",
            isLoading: true,
          });
          figma.notify("⏳ Importing generated Design...");

          const newNode = figma.createNodeFromSvg(result.svg);

          if (!newNode) {
            throw new Error(
              "Figma importer failed to create a node from the SVG content. The SVG might be invalid."
            );
          }

          newNode.name = result.frameName;

          // Get the parent of the target frame
          const parent = targetFrameNode.parent;

          if (parent) {
            // Get the index of the target frame within its parent's children
            const index = parent.children.indexOf(targetFrameNode);

            // Insert the new node at the same position as the target frame
            parent.insertChild(index, newNode);

            // Remove the old target frame
            targetFrameNode.remove();

            console.log(
              `Successfully replaced frame ${figmaFrameId} with new node ${newNode.id}`
            );
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            figma.notify("✅ New design generated successfully!");
            figma.ui.postMessage({ type: "creation-success" });
          } else {
            // Handle case where targetFrameNode has no parent (e.g., it's a top-level node)
            // In this scenario, we might want to just add it to the current page and then remove the old one.
            // Or, depending on desired behavior, throw an error if top-level replacement isn't supported.
            // For simplicity, let's assume it should always have a parent in this context or add it to current page.
            figma.currentPage.appendChild(newNode);
            targetFrameNode.remove();
            console.log(
              `Successfully replaced top-level frame ${figmaFrameId} with new node ${newNode.id}`
            );
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            figma.notify("✅ New design generated successfully!");
            figma.ui.postMessage({ type: "creation-success" });
          }
        } else if (mode === "modify") {
          const originalElement = await figma.getNodeByIdAsync(
            originalElementId
          );
          if (!originalElement || originalElement.removed) {
            throw new Error(
              `Original element (ID: ${originalElementId}) not found or was removed. Cannot replace. Please reselect.`
            );
          }
          if (
            !originalElement.parent ||
            originalElement.parent.type === "PAGE"
          ) {
            throw new Error(`Cannot replace top-level elements directly.`);
          }

          figma.ui.postMessage({
            type: "status-update",
            text: "Importing modified element SVG...",
            isLoading: true,
          });
          figma.notify("⏳ Importing modified element SVG...");

          let newNode = null;
          try {
            newNode = figma.createNodeFromSvg(result.svg);

            if (!newNode) {
              throw new Error(
                "Figma importer failed to create a node from the element SVG content. The SVG might be invalid."
              );
            }
            newNode.name = `${originalElement.name} (AI Modified)`;

            const parent = originalElement.parent;
            const index = parent.children.indexOf(originalElement);
            if (index === -1) {
              throw new Error(
                "Internal Error: Could not find original element in its parent's children list."
              );
            }
            const originalX = originalElement.x;
            const originalY = originalElement.y;
            const originalWidth = originalElement.width;
            const originalHeight = originalElement.height;
            const originalConstraints = originalElement.constraints;

            parent.insertChild(index + 1, newNode);
            newNode.x = originalX;
            newNode.y = originalY;

            try {
              if (originalConstraints) {
                newNode.constraints = originalConstraints;
              }
            } catch (constraintError) {
              console.warn(
                `Could not apply constraints: ${constraintError.message}`
              );
            }

            if (newNode.resize) {
              if (newNode.width > 0 && newNode.height > 0) {
                newNode.resize(originalWidth, originalHeight);
              } else {
                console.warn(
                  "New SVG node has zero dimensions, cannot resize to original."
                );
              }
            } else {
              console.warn("New SVG node does not support resize operation.");
            }

            originalElement.remove();
            console.log(
              `Successfully replaced element ${originalElementId} with new node: ${newNode.id}`
            );
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            figma.notify("✅ Element successfully modified!");
            figma.ui.postMessage({ type: "modification-success" });
          } catch (error) {
            // Cleanup if newNode was partially added
            if (
              newNode &&
              !newNode.removed &&
              newNode.parent !== originalElement?.parent
            ) {
              try {
                newNode.remove();
              } catch (cleanupError) {
                console.warn(
                  "Cleanup failed for partially added node:",
                  cleanupError
                );
              }
            }
            throw error; // Re-throw to be caught by outer catch
          }
        } else {
          throw new Error(`Unexpected 'svg' result for mode: ${mode}`);
        }
      } else if (result.mode === "answer") {
        // For answer mode, post the answer back to the UI
        figma.ui.postMessage({ type: "answer", answer: result.answer });
      } else {
        throw new Error(
          `Internal Error: Unknown success mode "${result.mode}" received from backend.`
        );
      }
    } catch (error) {
      console.error("AI Generation/Figma Action Error:", error);
      const errorMsg = `Error: ${
        error.message || "Unknown error during AI generation or Figma action."
      }`;
      figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
      figma.ui.postMessage({ type: "modification-error", error: errorMsg }); // Generic error for UI
    } finally {
      isProcessing = false;
    }
  }

  // Removed handlers for finalize-creation and replace-element-with-svg as code.js handles them directly
  else if (
    msg.type === "finalize-creation" ||
    msg.type === "replace-element-with-svg"
  ) {
    console.warn(
      `Code.js received deprecated message type "${msg.type}". Ignoring.`
    );
    isProcessing = false;
  }
  // Removed specific backend-error as it's now handled by backend-response-error
  else if (msg.type === "backend-error") {
    console.warn(
      `Code.js received deprecated message type "backend-error". Use "backend-response-error" instead. Ignoring.`
    );
    figma.notify(`❌ Error: ${msg.error}`, { error: true, timeout: 5000 });
    isProcessing = false;
  }
  // Removed the redundant 'answer' case handler, code.js sends "answer" as a result now.
  else if (msg.type === "answer") {
    console.warn(
      "Code.js received unexpected 'answer' message from UI. Ignoring."
    );
  } else {
    console.warn("Unknown message type received from UI:", msg.type);
    // Even if unknown, ensure processing flag is reset eventually
    isProcessing = false;
  }
};

// Removed setTimeout, as `auth-state-changed` message from UI will trigger initial selection update after auth.
// setTimeout(() => { figma.trigger('selectionchange'); }, 50);

console.log(
  "Figma AI Design Assistant plugin code (Backend + Auth Version) loaded."
);
