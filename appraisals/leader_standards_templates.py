"""Fixed Headteachers' Standards self-review content for senior leaders.

Source: "Growth Model Self Review Document Senior Leaders.docx" (Headteachers'
Standards 2020). Seeded into `LeaderStandard` rows per review (see
`LeaderReview.seed_standards`). Kept here as module data, mirroring the
`self_review_templates.py` pattern used by the teaching/support self-reviews.

`HEADTEACHER_STANDARDS` is a list of (number, title, descriptors) tuples:
- `number`      — the standard's 1-10 position (stable key + uniqueness).
- `title`       — the standard's name.
- `descriptors` — tuple of read-only prompt statements. Unlike the
  teaching/support bullets, these are NOT individually scored; the whole
  standard carries a single score, a "Not in Job Role" toggle and a free-text
  Examples box.

`ETHICS_CONTENT` is the Section 1 (Ethics and Professional Conduct) reference
text, shown read-only with no input fields. It is a list of (heading, bullets)
blocks rendered from this constant (never persisted).
"""

# Section 1: Ethics and Professional Conduct — static reference content.
ETHICS_CONTENT = [
    (
        "Headteachers uphold and demonstrate the seven principles of public life:",
        (
            "Selflessness",
            "Integrity",
            "Objectivity",
            "Accountability",
            "Openness",
            "Honesty",
            "Leadership",
        ),
    ),
    (
        "Headteachers uphold public trust in school leadership and maintain high "
        "standards of ethics and behaviour. Both within and outside school, "
        "headteachers:",
        (
            "Build relationships rooted in mutual respect, and at all times "
            "observe proper boundaries appropriate to their professional position",
            "Show tolerance of and respect for the rights of others, recognising "
            "differences and respecting cultural diversity within contemporary "
            "Britain",
            "Uphold fundamental British values, including democracy, the rule of "
            "law, individual liberty and mutual respect, and tolerance of those "
            "with different faiths and beliefs",
            "Ensure that personal beliefs are not expressed in ways which exploit "
            "their position, pupils' vulnerability or might lead pupils to break "
            "the law",
        ),
    ),
    (
        "As leaders of their school community and profession, headteachers:",
        (
            "Serve in the best interests of the school's pupils",
            "Conduct themselves in a manner compatible with their influential "
            "position in society by behaving ethically, fulfilling their "
            "professional responsibilities and modelling the behaviour of a good "
            "citizen",
            "Uphold their obligation to give account and accept responsibility",
            "Know, understand, and act within the statutory frameworks which set "
            "out their professional duties and responsibilities",
            "Take responsibility for their own continued professional "
            "development, engaging critically with educational research",
            "Make a positive contribution to the wider education system",
        ),
    ),
]


# Section 2: The 10 Headteacher's Standards — each scored individually.
HEADTEACHER_STANDARDS = [
    (
        1,
        "School Culture",
        (
            "Establish and sustain the school's ethos and strategic direction in "
            "partnership with those responsible for governance and through "
            "consultation with the school community",
            "Create a culture where pupils experience a positive and enriching "
            "school life",
            "Uphold ambitious educational standards which prepare pupils from all "
            "backgrounds for their next phase of education and life",
            "Promote positive and respectful relationships across the school "
            "community and a safe, orderly and inclusive environment",
            "Ensure a culture of high staff professionalism",
        ),
    ),
    (
        2,
        "Teaching",
        (
            "Establish and sustain high-quality, expert teaching across all "
            "subjects and phases, built on an evidence-informed understanding of "
            "effective teaching and how pupils learn",
            "Ensure teaching is underpinned by high levels of subject expertise "
            "and approaches which respect the distinct nature of subject "
            "disciplines or specialist domains",
            "Ensure effective use is made of formative assessment",
        ),
    ),
    (
        3,
        "Curriculum and Assessment",
        (
            "Ensure a broad, structured and coherent curriculum entitlement which "
            "sets out the knowledge, skills and values that will be taught",
            "Establish effective curricular leadership, developing subject leaders "
            "with high levels of relevant expertise with access to professional "
            "networks and communities",
            "Ensure that all pupils are taught to read through the provision of "
            "evidence-informed approaches to reading, particularly the use of "
            "systematic synthetic phonics in schools that teach early reading",
            "Ensure valid, reliable and proportionate approaches are used when "
            "assessing pupils' knowledge and understanding of the curriculum",
        ),
    ),
    (
        4,
        "Behaviour",
        (
            "Establish and sustain high expectations of behaviour for all pupils, "
            "built upon relationships, rules and routines, which are understood "
            "clearly by all staff and pupils",
            "Ensure high standards of pupil behaviour and courteous conduct in "
            "accordance with the school's behaviour policy",
            "Implement consistent, fair and respectful approaches to managing "
            "behaviour",
            "Ensure that adults within the school model and teach the behaviour of "
            "a good citizen",
        ),
    ),
    (
        5,
        "Additional and Special Educational Needs and Disabilities",
        (
            "Ensure the school holds ambitious expectations for all pupils with "
            "additional and special educational needs and disabilities",
            "Establish and sustain culture and practices that enable pupils to "
            "access the curriculum and learn effectively",
            "Ensure the school works effectively in partnership with parents, "
            "carers and professionals, to identify the additional needs and "
            "special educational needs and disabilities of pupils, providing "
            "support and adaptation where appropriate",
            "Ensure the school fulfils its statutory duties with regard to the "
            "SEND code of practice",
        ),
    ),
    (
        6,
        "Professional Development",
        (
            "Ensure staff have access to high-quality, sustained professional "
            "development opportunities, aligned to balance the priorities of "
            "whole-school improvement, team and individual needs",
            "Prioritise the professional development of staff, ensuring effective "
            "planning, delivery and evaluation which is consistent with the "
            "approaches laid out in the standard for teachers' professional "
            "development",
            "Ensure that professional development opportunities draw on expert "
            "provision from beyond the school, as well as within it, including "
            "nationally recognised career and professional frameworks and "
            "programmes to build capacity and support succession planning",
        ),
    ),
    (
        7,
        "Organisational Management",
        (
            "Ensure the protection and safety of pupils and staff through "
            "effective approaches to safeguarding, as part of the duty of care",
            "Prioritise and allocate financial resources appropriately, ensuring "
            "efficiency, effectiveness and probity in the use of public funds",
            "Ensure staff are deployed and managed well with due attention paid to "
            "workload",
            "Establish and oversee systems, processes and policies that enable the "
            "school to operate effectively and efficiently",
            "Ensure rigorous approaches to identifying, managing and mitigating "
            "risk",
        ),
    ),
    (
        8,
        "Continuous School Improvement",
        (
            "Make use of effective and proportional processes of evaluation to "
            "identify and analyse complex or persistent problems and barriers "
            "which limit school effectiveness, and identify priority areas for "
            "improvement",
            "Develop appropriate evidence-informed strategies for improvement as "
            "part of well-targeted plans which are realistic, timely, "
            "appropriately sequenced and suited to the school's context",
            "Ensure careful and effective implementation of improvement "
            "strategies, which lead to sustained school improvement over time",
        ),
    ),
    (
        9,
        "Working In Partnership",
        (
            "Forge constructive relationships beyond the school, working in "
            "partnership with parents, carers and the local community",
            "Commit their school to work successfully with other schools and "
            "organisations in a climate of mutual challenge and support",
            "Establish and maintain working relationships with fellow "
            "professionals and colleagues across other public services to improve "
            "educational outcomes for all pupils",
        ),
    ),
    (
        10,
        "Governance and Accountability",
        (
            "Understand and welcome the role of effective governance, upholding "
            "their obligation to give account and accept responsibility",
            "Establish and sustain professional working relationship with those "
            "responsible for governance",
            "Ensure that staff know and understand their professional "
            "responsibilities and are held to account",
            "Ensure the school effectively and efficiently operates within the "
            "required regulatory frameworks and meets all statutory duties",
        ),
    ),
]
