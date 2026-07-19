CONTROL_CENTER_STYLESHEET = """
QMainWindow, #AppShell, #Content { background: #f4f6f8; color: #172033; }
#Sidebar { background: #111827; color: #f8fafc; }
#BrandMark { color: #93c5fd; font-size: 12px; font-weight: 800; letter-spacing: 3px; }
#BrandTitle { color: #ffffff; font-size: 23px; font-weight: 700; }
#BrandSubtitle { color: #94a3b8; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
#Navigation { background: transparent; color: #cbd5e1; outline: 0; }
#Navigation::item { padding: 12px 13px; border-radius: 8px; }
#Navigation::item:hover { background: #1f2937; color: #ffffff; }
#Navigation::item:selected { background: #2563eb; color: #ffffff; }
#SafetyPanel { background: #172033; border: 1px solid #29364b; border-radius: 10px; }
#SafetyTitle { color: #86efac; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
#SafetyText { color: #aebbd0; font-size: 11px; }
#PageTitle { color: #111827; font-size: 25px; font-weight: 700; }
#PageSubtitle { color: #64748b; font-size: 12px; }
#ProjectSelector, #RefreshButton { min-height: 34px; border-radius: 7px; }
#ProjectSelector { background: #ffffff; border: 1px solid #d7dde6; padding: 0 10px; }
#RefreshButton { background: #ffffff; border: 1px solid #cbd5e1; padding: 0 14px; color: #1e293b; }
#RefreshButton:hover { background: #eef2f7; }
#RefreshButton:disabled { color: #94a3b8; background: #eef2f7; }
#StatusCard, #HeroPanel, #RuntimeCard, #ControlPanel, #PlaceholderPanel,
#OperationHeroPanel, #OperationFlowPanel, #OperationDetailsPanel, #HistoryHeroPanel,
#HistoryFiltersPanel, #HistoryDetailsPanel, #DiagnosticsHeroPanel,
#DiagnosticsToolbar {
    background: #ffffff; border: 1px solid #dfe5ec; border-radius: 12px;
}
#StatusCardTitle, #RuntimeCardTitle, #ControlTitle, #OperationSectionTitle,
#HistorySectionTitle { color: #64748b; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
#StatusCardValue, #RuntimeCardValue { color: #111827; font-size: 19px; font-weight: 700; }
#StatusCardDetail, #RuntimeCardDetail, #ControlDescription, #HeroText, #PlaceholderText,
#OperationFeedback, #OperationFieldLabel, #HistoryFeedback, #DiagnosticsHint,
#DiagnosticsFeedback { color: #64748b; font-size: 11px; }
#HeroTitle, #PlaceholderTitle { color: #172033; font-size: 18px; font-weight: 700; }
#OverallStatus, #OperationState, #DiagnosticsState {
    color: #1d4ed8; background: #eff6ff; border: 1px solid #bfdbfe;
    border-radius: 7px; padding: 6px 10px; font-size: 11px; font-weight: 800;
}
#OperationFlowSummary { color: #334155; font-size: 11px; }
#OperationFlowStep {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
}
#OperationFlowStepTitle { color: #172033; font-size: 11px; font-weight: 700; min-width: 145px; }
#OperationFlowStepDetail { color: #64748b; font-size: 10px; }
#OperationFlowStepStatus {
    color: #475569; background: #e2e8f0; border-radius: 6px; padding: 4px 7px;
    font-size: 9px; font-weight: 800;
}
#OperationFlowStep[flowStatus="active"] { background: #eff6ff; border-color: #bfdbfe; }
#OperationFlowStep[flowStatus="active"] #OperationFlowStepStatus { color: #1d4ed8; background: #dbeafe; }
#OperationFlowStep[flowStatus="success"] { background: #f0fdf4; border-color: #bbf7d0; }
#OperationFlowStep[flowStatus="success"] #OperationFlowStepStatus { color: #166534; background: #dcfce7; }
#OperationFlowStep[flowStatus="failed"] { background: #fef2f2; border-color: #fecaca; }
#OperationFlowStep[flowStatus="failed"] #OperationFlowStepStatus { color: #991b1b; background: #fee2e2; }
#OperationFieldValue, #HistoryDetails, #DiagnosticsDetails {
    color: #1e293b; font-size: 11px; font-family: Consolas;
}
#RefreshStatusButton, #StartButton, #StopButton, #RearmButton, #RefreshOperationButton,
#RefreshHistoryButton, #LoadMoreHistoryButton, #CollectDiagnosticsButton,
#ExportDiagnosticsButton {
    min-height: 34px; border-radius: 7px; padding: 0 14px; font-weight: 600;
}
#RefreshStatusButton, #RefreshOperationButton, #RefreshHistoryButton,
#LoadMoreHistoryButton, #CollectDiagnosticsButton, #ExportDiagnosticsButton {
    background: #ffffff; border: 1px solid #cbd5e1; color: #1e293b;
}
#StartButton { background: #166534; border: 1px solid #14532d; color: #ffffff; }
#StopButton { background: #991b1b; border: 1px solid #7f1d1d; color: #ffffff; }
#RearmButton { background: #1d4ed8; border: 1px solid #1e40af; color: #ffffff; }
QPushButton:disabled { background: #e5e7eb; border-color: #d1d5db; color: #9ca3af; }
#ArmMinutesSpin { min-height: 32px; min-width: 82px; }
#ArmMinutesLabel { color: #475569; font-size: 11px; }
#ControlFeedback { color: #475569; font-size: 11px; }
#StatusLine { color: #64748b; font-size: 11px; }
#HistoryTable, #DiagnosticsTable {
    background: #ffffff; border: 1px solid #dfe5ec; gridline-color: #e5e7eb;
}
#HistorySessionFilter, #HistoryCommandFilter, #HistoryLimitSpin {
    min-height: 32px; background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px;
}
"""
