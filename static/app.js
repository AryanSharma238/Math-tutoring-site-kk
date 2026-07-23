// Sidebar collapse
(function () {
  const btn = document.getElementById("collapseBtn");
  const sidebar = document.getElementById("sidebar");
  if (!btn || !sidebar) return;

  if (localStorage.getItem("sidebarCollapsed") === "true") {
    sidebar.classList.add("collapsed");
  }

  btn.addEventListener("click", () => {
    sidebar.classList.toggle("collapsed");
    localStorage.setItem("sidebarCollapsed", sidebar.classList.contains("collapsed"));
  });
})();

// Interactive quiz renderer (used on the take-quiz page)
function renderQuizQuestions(questions, container) {
  container.innerHTML = questions
    .map((p, pi) => {
      const choices = Array.isArray(p.choices) ? p.choices : [];
      const choicesHtml = choices
        .map(
          (c, ci) => `
            <button class="choice-btn" data-problem="${pi}" data-choice="${ci}">
              <span class="choice-label">${escapeHtml(c.label || "")}</span>
              ${escapeHtml(c.text || "")}
            </button>
          `
        )
        .join("");

      return `
        <div class="quiz-question" data-problem="${pi}">
          <span class="num">${pi + 1}.</span> ${escapeHtml(p.question || "")}
          <div class="quiz-choices">${choicesHtml}</div>
          <p class="quiz-feedback" id="feedback-${pi}"></p>
          <button class="steps-toggle" data-problem="${pi}">Show step-by-step solution</button>
          <div class="steps" id="steps-${pi}" hidden>${escapeHtml(p.solution || "")}</div>
        </div>
      `;
    })
    .join("");

  container.addEventListener("click", (e) => {
    const choiceBtn = e.target.closest(".choice-btn");
    if (choiceBtn) {
      handleQuizChoiceClick(questions, choiceBtn);
      return;
    }
    const stepsBtn = e.target.closest(".steps-toggle");
    if (stepsBtn) {
      const pi = stepsBtn.dataset.problem;
      const stepsEl = document.getElementById(`steps-${pi}`);
      stepsEl.hidden = !stepsEl.hidden;
      stepsBtn.textContent = stepsEl.hidden ? "Show step-by-step solution" : "Hide step-by-step solution";
    }
  });
}

function handleQuizChoiceClick(questions, choiceBtn) {
  const pi = parseInt(choiceBtn.dataset.problem, 10);
  const ci = parseInt(choiceBtn.dataset.choice, 10);
  const problem = questions[pi];
  const choice = problem.choices[ci];
  const problemEl = document.querySelector(`.quiz-question[data-problem="${pi}"]`);
  const feedbackEl = document.getElementById(`feedback-${pi}`);

  if (problemEl.dataset.answered === "true") return;
  problemEl.dataset.answered = "true";

  problemEl.querySelectorAll(".choice-btn").forEach((btn) => {
    const c = problem.choices[parseInt(btn.dataset.choice, 10)];
    if (c.correct) btn.classList.add("correct");
    btn.disabled = true;
  });

  if (choice.correct) {
    feedbackEl.textContent = "Correct!";
    feedbackEl.className = "quiz-feedback correct-text";
  } else {
    choiceBtn.classList.add("incorrect");
    feedbackEl.textContent = choice.explanation || "That's not right.";
    feedbackEl.className = "quiz-feedback incorrect-text";
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// Reveal-on-scroll: sections stay hidden until scrolled into view.
(function () {
  const targets = document.querySelectorAll(".reveal-on-scroll");
  if (!targets.length) return;
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.15 }
  );
  targets.forEach((el) => observer.observe(el));
})();
