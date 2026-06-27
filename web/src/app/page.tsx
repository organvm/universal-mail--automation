import fs from 'fs/promises';
import path from 'path';

type Tier = 1 | 2 | 3 | 4;
type LabelHistory = Record<string, number>;

interface LabelerState {
  history: LabelHistory;
}

function toHistory(value: unknown): LabelHistory {
  if (!value || typeof value !== "object") {
    return {};
  }

  return Object.entries(value as Record<string, unknown>).reduce<LabelHistory>(
    (history, [label, count]) => {
      const numericCount = Number(count);
      if (Number.isFinite(numericCount)) {
        history[label] = numericCount;
      }
      return history;
    },
    {},
  );
}

function parseState(value: unknown): LabelerState {
  if (!value || typeof value !== "object") {
    return { history: {} };
  }

  return {
    history: toHistory((value as { history?: unknown }).history),
  };
}

function messageFor(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export default async function Dashboard() {
  let stats: LabelerState = { history: {} };
  let error: string | null = null;

  try {
    const statePath = path.join(process.cwd(), '../labeler_state.json');
    const content = await fs.readFile(statePath, 'utf8');
    stats = parseState(JSON.parse(content) as unknown);
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") {
      error = messageFor(err);
    }
  }

  // Pre-calculated mapping based on python core/rules.py
  const labelToTier: Record<string, Tier> = {
    "Dev/GitHub": 2, "Dev/Code-Review": 2, "Dev/Infrastructure": 3, "Dev/GameDev": 3, 
    "AI/Services": 3, "AI/Grok": 3, "AI/Data Exports": 2, "Finance/Banking": 1, 
    "Finance/Payments": 2, "Finance/Tax": 2, "Tech/Security": 1, "Tech/Google": 2, 
    "Shopping": 4, "Personal/Health": 2, "Social/LinkedIn": 3, "Travel": 2, 
    "Entertainment": 4, "Education/Research": 3, "Professional/Jobs": 2, 
    "Professional/Legal": 2, "Services/Domain": 2, "Notification": 3, "Marketing": 4, 
    "Tech/Storage": 3, "Personal/Government": 1, "Personal": 1, "Awaiting Reply": 2, 
    "Misc/Other": 4
  };
  
  const tierNames: Record<Tier, string> = {1: "Critical", 2: "Important", 3: "Delegate", 4: "Reference"};
  const tiers: Tier[] = [1, 2, 3, 4];
  
  const tierCounts: Record<Tier, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };
  
  for (const [label, count] of Object.entries(stats.history)) {
    const tier = labelToTier[label] || 4; // Default to 4 if unknown
    tierCounts[tier] += count;
  }

  const sortedHistory = Object.entries(stats.history).sort((a, b) => b[1] - a[1]);

  return (
    <main className="min-h-screen p-8 bg-gray-50 text-gray-900 font-sans">
      <div className="max-w-4xl mx-auto space-y-8">
        <header>
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">Inbox Health Dashboard</h1>
          <p className="mt-2 text-gray-600">Overview of email processing via universal-mail-automation engine.</p>
        </header>

        {error ? (
          <div className="p-4 bg-red-50 text-red-700 border border-red-200 rounded-lg">
            Failed to load state: {error}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {tiers.map(tier => (
                <div key={tier} className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm">
                  <h3 className="text-sm font-medium text-gray-500 uppercase tracking-wider">
                    Tier {tier} - {tierNames[tier]}
                  </h3>
                  <div className="mt-2 text-4xl font-bold text-gray-900">
                    {tierCounts[tier].toLocaleString()}
                  </div>
                </div>
              ))}
            </div>

            <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
              <div className="px-6 py-4 border-b border-gray-200 bg-gray-50">
                <h3 className="text-lg font-semibold text-gray-900">Label Distribution</h3>
              </div>
              <ul className="divide-y divide-gray-200">
                {sortedHistory.map(([label, count]) => (
                  <li key={label} className="px-6 py-4 flex justify-between items-center hover:bg-gray-50 transition-colors">
                    <span className="font-medium text-gray-700">{label}</span>
                    <span className="text-gray-900 font-semibold bg-gray-100 px-3 py-1 rounded-full text-sm">
                      {count.toLocaleString()}
                    </span>
                  </li>
                ))}
                {sortedHistory.length === 0 && (
                  <li className="px-6 py-8 text-center text-gray-500">No data available</li>
                )}
              </ul>
            </div>
          </>
        )}
      </div>
    </main>
  );
}
