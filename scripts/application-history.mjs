function applicationKey(row) {
  if (!row.source || !row.jr) return null;
  return `${row.source.trim().toLowerCase()}:${row.jr.trim().toUpperCase()}`;
}

export function attachApplications(rows, applications) {
  const latest = new Map();
  for (const application of applications) {
    const key = applicationKey(application);
    if (!key) continue;
    const previous = latest.get(key);
    if (!previous || application.applicationSubmittedDate > previous.applicationSubmittedDate) {
      latest.set(key, application);
    }
  }
  return rows.map((row) => {
    const application = latest.get(applicationKey(row));
    return {
      ...row,
      applicationStatus: application?.applicationStatus ?? null,
      applicationSubmittedDate: application?.applicationSubmittedDate ?? null,
    };
  });
}

export async function queryApplicationHistory(connection) {
  const [rows] = await connection.execute(`
    SELECT source, jr, title, status AS applicationStatus,
      DATE_FORMAT(submitted_date, '%Y-%m-%d') AS applicationSubmittedDate
    FROM job_applications
    ORDER BY submitted_date DESC, updated_at DESC, source, jr
  `);
  return rows;
}
